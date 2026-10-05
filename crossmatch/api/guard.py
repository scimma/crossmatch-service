"""Per-request cost bound for API views (KTD12, R29, R32).

``api_guard`` wraps an API view so its whole run happens inside a read-only
transaction under a request budget (``settings.API_REQUEST_BUDGET_SECONDS``,
read at call time) with a monotonic wall-clock deadline that covers Python work
as well as SQL.

Service code marks its work with two helpers, which find the active guard
through a context variable, so nothing has to be threaded through call
signatures, and which are no-ops when no guard is active (shell, tests):

* ``with sql_phase():`` around each block of queries. On entry it checks the
  deadline and resets the transaction-local ``statement_timeout`` to the time
  remaining, so Postgres cancels a statement that would outrun the request; the
  time inside counts as SQL time in the request log.
* ``check_deadline()`` inside Python loops over many inputs or rows, so an
  overrun stops the work rather than finishing it.

Errors are classified deadline-first: a query cancellation (SQLSTATE 57014), a
``DeadlineExceeded``, or any database error raised once the deadline has passed
returns ``400 query_too_expensive`` (``retryable: false``: the same request
would overrun again). Only other connection errors return ``503
service_unavailable`` with ``Retry-After`` (``retryable: true``). Both bodies
carry fixed messages; the raw exception text, which can name the database host
and port, goes only to the log. A view that must report an outage itself (the
status endpoint) passes ``map_unavailable=False`` and the connection error
propagates to it. An ``ApiError`` raised by the view becomes its error response.

Each request logs one ``api request`` line with its phase timings: SQL seconds
and phase count, Python seconds (the rest of the request), total seconds, and
response bytes.

The core of the guard is ``run_guarded`` (KTD15 of the MCP chat connector
plan), which runs any callable, not only a view, under a ``RequestGuard``:
the budget, the read-only transaction, the per-phase ``statement_timeout``,
and the same classification, raised as ``ApiError``. ``api_guard`` is built on
it; a non-view caller (the MCP dispatcher) creates the guard, calls
``run_guarded``, and reads ``guard.timings()`` and ``guard.error`` for its log
line whether the call returned or raised.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

from django.conf import settings
from django.db import DatabaseError, InterfaceError, OperationalError
from django.http import HttpRequest, HttpResponse

from api.contract import ErrorCode, error_response
from api.errors import ApiError
from core.db import read_only_transaction, set_statement_timeout
from core.log import get_logger

logger = get_logger(__name__)

T = TypeVar('T')

#: ``Retry-After`` seconds on a ``503 service_unavailable``.
UNAVAILABLE_RETRY_AFTER_SECONDS = 30

#: SQLSTATE ``query_canceled``: Postgres cancelled a statement (its
#: ``statement_timeout`` fired, or it was cancelled from outside).
_QUERY_CANCELED = '57014'

_TOO_EXPENSIVE_MESSAGE = (
    'The request exceeded the per-request time budget. Retrying the same request '
    'will not help; send fewer inputs or a smaller radius.'
)
_UNAVAILABLE_MESSAGE = 'The service database is temporarily unavailable; retry later.'


class DeadlineExceeded(Exception):
    """The request's wall-clock deadline has passed."""


class RequestGuard:
    """The budget, deadline, and phase timings of one guarded request.

    Attributes:
        budget: The request budget, in seconds.
        started: ``time.monotonic()`` when the request started.
        deadline: ``time.monotonic()`` value after which the request is over budget.
        sql_seconds: Time spent inside ``sql_phase`` blocks so far.
        sql_phases: Number of ``sql_phase`` blocks entered.
        error: The raw error text of a classified failure (``'deadline
            exceeded'`` or the database error's message), set by
            ``run_guarded``; for the log only, never for a response body.
    """

    def __init__(self, budget: float | None = None) -> None:
        """Start the clock.

        Args:
            budget: The request budget, in seconds. Defaults to
                ``settings.API_REQUEST_BUDGET_SECONDS``, read now.
        """
        if budget is None:
            budget = float(settings.API_REQUEST_BUDGET_SECONDS)
        self.budget = budget
        self.started = time.monotonic()
        self.deadline = self.started + budget
        self.sql_seconds = 0.0
        self.sql_phases = 0
        self.error: str | None = None

    def remaining(self) -> float:
        """Seconds left before the deadline (negative once it has passed)."""
        return self.deadline - time.monotonic()

    def expired(self) -> bool:
        """Whether the deadline has passed."""
        return self.remaining() <= 0

    def timings(self) -> dict[str, Any]:
        """The phase timings so far, as fields for a log line.

        Returns:
            ``budget_seconds``, ``total_seconds`` (since the guard started),
            ``sql_seconds``, ``sql_phases``, and ``python_seconds`` (total
            minus SQL), with seconds rounded to 0.1 ms.
        """
        total = time.monotonic() - self.started
        return {
            'budget_seconds': self.budget,
            'total_seconds': round(total, 4),
            'sql_seconds': round(self.sql_seconds, 4),
            'sql_phases': self.sql_phases,
            'python_seconds': round(max(0.0, total - self.sql_seconds), 4),
        }

    def check_deadline(self) -> None:
        """Raise if the deadline has passed.

        Raises:
            DeadlineExceeded: If the deadline has passed.
        """
        if self.expired():
            raise DeadlineExceeded

    @contextmanager
    def sql_phase(self) -> Iterator[None]:
        """Bound and time one block of queries.

        Raises:
            DeadlineExceeded: If the deadline has already passed on entry.
        """
        self.check_deadline()
        set_statement_timeout(self.remaining())
        self.sql_phases += 1
        began = time.monotonic()
        try:
            yield
        finally:
            self.sql_seconds += time.monotonic() - began


_current: ContextVar[RequestGuard | None] = ContextVar('api_request_guard', default=None)


def current_guard() -> RequestGuard | None:
    """The guard of the request being served, or ``None`` outside one."""
    return _current.get()


def check_deadline() -> None:
    """Raise if the active request is over budget; a no-op outside a guard.

    Raises:
        DeadlineExceeded: If the active request's deadline has passed.
    """
    guard = _current.get()
    if guard is not None:
        guard.check_deadline()


@contextmanager
def sql_phase() -> Iterator[None]:
    """Run a block of queries under the active request's remaining budget.

    Outside a guard this is a no-op, so service functions stay callable from a
    shell or test without one.

    Raises:
        DeadlineExceeded: If the active request's deadline has already passed.
    """
    guard = _current.get()
    if guard is None:
        yield
        return
    with guard.sql_phase():
        yield


def _is_query_cancel(exc: BaseException) -> bool:
    """Whether a database error is a Postgres statement cancellation."""
    for err in (exc, exc.__cause__):
        if getattr(err, 'sqlstate', None) == _QUERY_CANCELED:
            return True
    return False


def _too_expensive() -> ApiError:
    return ApiError(
        _TOO_EXPENSIVE_MESSAGE,
        code=ErrorCode.QUERY_TOO_EXPENSIVE,
        status=400,
        retryable=False,
    )


def _unavailable() -> ApiError:
    return ApiError(
        _UNAVAILABLE_MESSAGE,
        code=ErrorCode.SERVICE_UNAVAILABLE,
        status=503,
        retryable=True,
        retry_after=UNAVAILABLE_RETRY_AFTER_SECONDS,
    )


def run_guarded(
    guard: RequestGuard,
    fn: Callable[[], T],
    *,
    map_unavailable: bool = True,
    check_result: Callable[[T], bool] | None = None,
) -> T:
    """Run ``fn`` under ``guard``: the request budget and a read-only transaction.

    ``fn`` runs with ``guard`` active (so ``sql_phase`` and ``check_deadline``
    in service code find it), inside a read-only transaction whose
    ``statement_timeout`` starts at the time remaining. After the transaction
    ends, the deadline is checked once more, so a result finished over budget
    is still an overrun. Errors are classified deadline-first, as described in
    the module docstring. An ``ApiError`` raised by ``fn`` propagates as is.

    Args:
        guard: The guard to run under; the caller keeps it to read
            ``guard.timings()`` and ``guard.error`` afterwards.
        fn: The work to run, taking no arguments.
        map_unavailable: When false, connection errors that are not budget
            overruns propagate unchanged instead of becoming
            ``service_unavailable``.
        check_result: When given, the final deadline check runs only if it
            returns true for the result (``api_guard`` skips it for error
            responses).

    Returns:
        What ``fn`` returned.

    Raises:
        ApiError: ``query_too_expensive`` on an overrun, ``service_unavailable``
            on a connection error (when ``map_unavailable``), or one raised by
            ``fn``.
        DatabaseError: Any other database error, unchanged.
    """
    token = _current.set(guard)
    try:
        with read_only_transaction(guard.remaining()):
            result = fn()
        if check_result is None or check_result(result):
            guard.check_deadline()
        return result
    except DeadlineExceeded:
        guard.error = 'deadline exceeded'
        raise _too_expensive() from None
    except DatabaseError as exc:
        guard.error = str(exc)
        if _is_query_cancel(exc) or guard.expired():
            raise _too_expensive() from exc
        if map_unavailable and isinstance(exc, (OperationalError, InterfaceError)):
            raise _unavailable() from exc
        raise
    finally:
        _current.reset(token)


def api_guard(
    view: Callable[..., HttpResponse] | None = None,
    *,
    map_unavailable: bool = True,
) -> Any:
    """Decorate an API view to run under the per-request cost bound.

    Usable bare (``@api_guard``) or with options
    (``@api_guard(map_unavailable=False)``).

    Args:
        view: The view function.
        map_unavailable: When false, connection errors that are not budget
            overruns propagate to the caller instead of becoming a 503 (for the
            status endpoint, which reports availability itself).

    Returns:
        The wrapped view, or a decorator when called with options only.
    """
    if view is None:
        return functools.partial(api_guard, map_unavailable=map_unavailable)

    @functools.wraps(view)
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        guard = RequestGuard()
        try:
            try:
                response = run_guarded(
                    guard,
                    lambda: view(request, *args, **kwargs),
                    map_unavailable=map_unavailable,
                    check_result=lambda resp: resp.status_code < 400,
                )
            except ApiError as exc:
                response = error_response(exc)
        except BaseException as exc:
            _log(view, guard, None, repr(exc))
            raise
        _log(view, guard, response, guard.error)
        return response

    return wrapped


def _log(
    view: Callable[..., HttpResponse],
    guard: RequestGuard,
    response: HttpResponse | None,
    error: str | None,
) -> None:
    """Log one request's phase timings (KTD12 step 7)."""
    fields: dict[str, Any] = {
        'view': view.__name__,
        'status': response.status_code if response is not None else None,
        **guard.timings(),
        'response_bytes': (
            len(response.content)
            if response is not None and not response.streaming
            else None
        ),
    }
    if error is None:
        logger.info('api request', **fields)
    else:
        logger.warning('api request', error=error, **fields)
