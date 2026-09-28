"""Database helpers shared by read-only callers (the replay export, the API guard).

``read_only_transaction`` runs the enclosed queries in a transaction Postgres
enforces as read-only, optionally under a transaction-local
``statement_timeout``. ``set_statement_timeout`` resets that timeout inside the
transaction, which the API guard (``api/guard.py``, KTD12) does before each SQL
phase so a request's statements share one budget.

Postgres ``transaction_timeout`` is deliberately not used: on overrun it ends
the whole session, which surfaces as a lost connection and would be
misreported as an outage rather than as an over-budget query.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection, transaction


def set_statement_timeout(seconds: float) -> None:
    """Set the current transaction's ``statement_timeout``.

    The value is transaction-local (``set_config(..., true)``), so it lapses at
    commit or rollback and never leaks onto a reused connection. It is rounded
    up to whole milliseconds and never below 1 ms, since 0 would disable the
    timeout.

    Args:
        seconds: The timeout, in seconds.
    """
    millis = max(1, math.ceil(seconds * 1000))
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, true)", [f'{millis}ms']
        )


@contextmanager
def read_only_transaction(
    statement_timeout_seconds: float | None = None,
) -> Iterator[None]:
    """Run the enclosed queries in a transaction Postgres enforces as read-only.

    Args:
        statement_timeout_seconds: When set, Postgres cancels any single query in
            the transaction that runs longer, so an export can never hold a long
            query against the PROD database.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
        if statement_timeout_seconds:
            set_statement_timeout(statement_timeout_seconds)
        yield
