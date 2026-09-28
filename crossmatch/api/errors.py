"""Shared exception types for the read-model API layer.

``InvalidQuery`` lives here rather than in ``service.py`` so both ``service.py``
and ``pagination.py`` can import it without a circular import: ``service.py``
imports the cursor codec from ``pagination.py``, so a top-level
``from api.service import InvalidQuery`` inside the codec would cycle. It is
re-exported from ``service.py`` for existing importers (e.g. ``api/views.py``).

``ApiError`` is the structured error of the response contract (KTD2): a fixed
``code``, a ``message``, the offending ``param`` or ``params``, and whether a
retry can help. Its body keeps the legacy ``error`` string, equal to
``message``, so clients that read ``error`` keep working (A4).
"""

from __future__ import annotations

from typing import Any, Sequence

# The error codes (KTD2). Kept here, not in ``api.contract``, so this module
# stays import-light for ``pagination.py``; ``api.contract.ErrorCode`` mirrors
# them as choices and the test suite pins the two together.
ERROR_CODES = (
    'invalid_parameter',
    'method_not_allowed',
    'query_too_expensive',
    'service_unavailable',
)


class ApiError(Exception):
    """A request-level API error, serialized as the contract's error body.

    Attributes:
        message: Human-readable reason; also the legacy ``error`` string.
        code: One of ``ERROR_CODES``.
        status: The HTTP status to return.
        param: The single offending parameter, if any.
        params: Several offending parameters, if any (mutually exclusive with
            ``param``).
        retryable: Whether the same request can succeed if retried.
        retry_after: Seconds for a ``Retry-After`` header, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int = 400,
        param: str | None = None,
        params: Sequence[str] | None = None,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        """Build the error.

        Raises:
            ValueError: If ``code`` is not a contract error code, or both
                ``param`` and ``params`` are given.
        """
        if code not in ERROR_CODES:
            raise ValueError(f'unknown API error code: {code!r}')
        if param is not None and params is not None:
            raise ValueError('pass param or params, not both')
        super().__init__(message)
        self.message = message
        self.code = str(code)
        self.status = status
        self.param = param
        self.params = list(params) if params is not None else None
        self.retryable = retryable
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        """The JSON error body.

        Returns:
            ``error`` (legacy, equal to ``message``), ``code``, ``message``,
            ``param`` or ``params`` when set, and ``retryable``.
        """
        body: dict[str, Any] = {
            'error': self.message,
            'code': self.code,
            'message': self.message,
        }
        if self.param is not None:
            body['param'] = self.param
        if self.params is not None:
            body['params'] = self.params
        body['retryable'] = self.retryable
        return body


class InvalidQuery(ApiError, ValueError):
    """A request parameter is invalid; the view maps this to HTTP 400.

    Still a ``ValueError`` and still constructible from a message alone, so
    existing raisers and handlers are unchanged.
    """

    def __init__(
        self,
        message: str,
        *,
        param: str | None = None,
        params: Sequence[str] | None = None,
    ) -> None:
        """Build a ``400 invalid_parameter`` error naming the parameter(s)."""
        super().__init__(
            message, code='invalid_parameter', status=400, param=param, params=params
        )
