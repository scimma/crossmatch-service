"""Rate limits and the concurrency cap on MCP tool calls (R12, R13; KTD4).

Every ``tools/call`` is admitted by ``admit``, on the shared Django cache
(Valkey), so all web replicas share one view of each caller:

* **Who the caller is.** The client IP is the ``MCP_CLIENT_IP_HEADER`` header
  (``X-Real-Ip``, set by Traefik), falling back to ``REMOTE_ADDR``. A caller in
  ``MCP_PROVIDER_CIDRS`` (a chat provider's egress range) is limited per
  verified session ID at ``MCP_SESSION_RATE``; a provider caller with no valid
  session ID falls into one aggregate bucket per provider range at
  ``MCP_PROVIDER_RATE``. Any other caller is limited per client IP at
  ``MCP_IP_RATE``, whatever session IDs it sends.
* **Rate.** Each rate is ``(requests per second, burst)``. The limit is a
  sliding-window counter over a window of ``burst / rate`` seconds holding at
  most ``burst`` requests, built only from the cache's atomic ``add``,
  ``incr`` and ``decr``: the Django cache has no compare-and-set, so a token
  bucket's read-modify-write could not be atomic. It allows the same burst and
  the same sustained rate as a token bucket, and refills more conservatively
  just after a burst. A refused call is not counted.
* **Concurrency.** A call holds one of ``MCP_MAX_CONCURRENT`` cluster-wide slot
  keys (``mcp:slot:<i>``) and one of ``MCP_MAX_CONCURRENT_PER_KEY`` slots of
  its limiter key, so one conversation cannot hold every slot. A slot is
  claimed with the atomic ``add`` and expires after the request budget plus a
  margin, so the slot of a worker killed mid-call frees itself; a finished
  call deletes its slot only if the slot still holds its own token.
* **Cache unreachable.** Both limits fail open with a warning; the request
  guard's budget still bounds each call.

An over-limit call raises ``Limited``, which the view turns into an
``isError`` tool result with a retry-after, never an HTTP 429.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest

from core.log import get_logger

logger = get_logger(__name__)

CLIENT_PROVIDER = 'provider'
CLIENT_DIRECT = 'direct'

#: Seconds a slot outlives the request budget before it expires on its own.
SLOT_MARGIN_SECONDS = 10

GLOBAL_SLOT_PREFIX = 'mcp:slot'


def _now() -> float:
    """The limiter's clock (wall time, shared across replicas)."""
    return time.time()


class Limited(Exception):
    """A call refused by a rate limit or the concurrency cap.

    Attributes:
        code: ``rate_limited`` or ``busy``.
        message: What limit was hit, for the model to relay.
        retry_after: Whole seconds to wait before retrying.
    """

    def __init__(self, code: str, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class Caller:
    """How one call is classified and limited.

    Attributes:
        client_class: ``provider`` or ``direct`` (logged, KTD14).
        key: The limiter key for the rate bucket and the per-key slots.
        rate: ``(requests per second, burst)``.
        subject: What the limit applies to, for the message.
    """

    client_class: str
    key: str
    rate: tuple[float, int]
    subject: str


def client_ip(request: HttpRequest) -> str:
    """The client IP of a request.

    Args:
        request: The request.

    Returns:
        The ``MCP_CLIENT_IP_HEADER`` header when present, else ``REMOTE_ADDR``
        (empty when neither is set).
    """
    forwarded = request.headers.get(settings.MCP_CLIENT_IP_HEADER, '').strip()
    return forwarded or request.META.get('REMOTE_ADDR', '')


def _provider_range(ip: str) -> str | None:
    """The ``MCP_PROVIDER_CIDRS`` entry containing ``ip``, if any."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for cidr in settings.MCP_PROVIDER_CIDRS:
        network = ipaddress.ip_network(cidr, strict=False)
        if address.version == network.version and address in network:
            return cidr
    return None


def identify_caller(ip: str, token: str | None) -> Caller:
    """Classify a caller and choose its limiter key and rate (KTD4).

    Args:
        ip: The client IP.
        token: The verified session token, or ``None``.

    Returns:
        A provider caller with a session is keyed on the session; one without
        on its provider range; any other caller on its IP.
    """
    provider_range = _provider_range(ip)
    if provider_range is None:
        return Caller(
            CLIENT_DIRECT, f'ip:{ip[:64]}', tuple(settings.MCP_IP_RATE), 'this client',
        )
    if token is not None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        return Caller(
            CLIENT_PROVIDER, f'session:{digest}', tuple(settings.MCP_SESSION_RATE),
            'this conversation',
        )
    return Caller(
        CLIENT_PROVIDER, f'provider:{provider_range}', tuple(settings.MCP_PROVIDER_RATE),
        'chat sessions without a session ID',
    )


def _cache_unavailable(exc: Exception) -> None:
    logger.warning('mcp limiter cache unavailable', error=repr(exc))


def _rate_retry_after(caller: Caller) -> int | None:
    """Count one call against the caller's bucket.

    Returns:
        ``None`` when the call is admitted, else whole seconds until it would
        be. The refused call is not counted.
    """
    rate, burst = caller.rate
    window = burst / rate
    now = _now()
    index = math.floor(now / window)
    elapsed = now - index * window
    current_key = f'mcp:rate:{caller.key}:{index}'
    previous_key = f'mcp:rate:{caller.key}:{index - 1}'
    cache.add(current_key, 0, timeout=math.ceil(2 * window) + 1)
    current = cache.incr(current_key)
    previous = cache.get(previous_key) or 0
    if current + previous * (1 - elapsed / window) <= burst:
        return None
    cache.decr(current_key)
    admitted = current - 1
    if admitted + 1 > burst:
        # Nothing more fits in this window: wait for it to roll over, then for
        # this window's count, now the previous one, to decay below the burst.
        wait = window - elapsed + max(0.0, window * (1 - (burst - 1) / admitted))
    else:
        wait = window * (1 - (burst - 1 - admitted) / previous) - elapsed
    return max(1, math.ceil(wait))


def slot_timeout() -> int:
    """Seconds a slot lives unless released: the request budget plus a margin."""
    return math.ceil(settings.API_REQUEST_BUDGET_SECONDS) + SLOT_MARGIN_SECONDS


def claim_slot(prefix: str, count: int, token: str, timeout: int) -> str | None:
    """Claim the first free slot of a set with the cache's atomic ``add``.

    Args:
        prefix: The slot set; slot keys are ``<prefix>:<i>``.
        count: How many slots the set has.
        token: This call's token, stored in the slot.
        timeout: Seconds until the slot frees itself.

    Returns:
        The claimed slot key, or ``None`` when every slot is held.
    """
    for i in range(count):
        key = f'{prefix}:{i}'
        if cache.add(key, token, timeout=timeout):
            return key
    return None


def release_slot(key: str, token: str) -> None:
    """Delete a slot only if it still holds ``token``.

    The compare and the delete are two cache operations: if the slot expired
    and another call claimed it in between, that call's slot is deleted early.
    That needs a call to outlive its slot's timeout, which the request budget
    makes rare, and costs at most one extra concurrent call.

    Args:
        key: The slot key.
        token: The token stored when the slot was claimed.
    """
    if cache.get(key) == token:
        cache.delete(key)


def _release_all(claimed: list[tuple[str, str]]) -> None:
    for key, token in claimed:
        try:
            release_slot(key, token)
        except Exception as exc:  # fail open (KTD4)
            _cache_unavailable(exc)


def _claim_slots(caller: Caller) -> list[tuple[str, str]]:
    """Claim a per-key slot and a cluster-wide slot.

    Returns:
        The claimed ``(key, token)`` pairs; empty when the cache is
        unreachable (fail open).

    Raises:
        Limited: ``busy`` when either slot set is full.
    """
    token = secrets.token_hex(8)
    timeout = slot_timeout()
    retry_after = math.ceil(settings.API_REQUEST_BUDGET_SECONDS)
    per_key = settings.MCP_MAX_CONCURRENT_PER_KEY
    claimed: list[tuple[str, str]] = []
    try:
        key = claim_slot(f'mcp:keyslot:{caller.key}', per_key, token, timeout)
        if key is None:
            raise Limited(
                'busy',
                f'{caller.subject[0].upper()}{caller.subject[1:]} already has '
                f'{per_key} tool calls in progress; retry in {retry_after} seconds.',
                retry_after,
            )
        claimed.append((key, token))
        key = claim_slot(GLOBAL_SLOT_PREFIX, settings.MCP_MAX_CONCURRENT, token, timeout)
        if key is None:
            raise Limited(
                'busy',
                'The service is busy answering other chat requests; '
                f'retry in {retry_after} seconds.',
                retry_after,
            )
        claimed.append((key, token))
    except Limited:
        _release_all(claimed)
        raise
    except Exception as exc:  # fail open (KTD4)
        _cache_unavailable(exc)
        _release_all(claimed)
        return []
    return claimed


@contextmanager
def admit(caller: Caller) -> Iterator[None]:
    """Admit one tool call under the rate limit and the concurrency cap.

    Args:
        caller: The classified caller (``identify_caller``).

    Yields:
        Nothing; the call runs inside, holding its slots, which are released
        on exit whether the call returned or raised.

    Raises:
        Limited: Before yielding, when the call is over a limit.
    """
    try:
        retry_after = _rate_retry_after(caller)
    except Exception as exc:  # fail open (KTD4)
        _cache_unavailable(exc)
        retry_after = None
    if retry_after is not None:
        rate, burst = caller.rate
        raise Limited(
            'rate_limited',
            f'Too many requests from {caller.subject}: the limit is {rate:g} per '
            f'second with bursts of {burst}; retry in {retry_after} seconds.',
            retry_after,
        )
    claimed = _claim_slots(caller)
    try:
        yield
    finally:
        _release_all(claimed)
