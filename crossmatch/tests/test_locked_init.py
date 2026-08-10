"""locked_init acquires the advisory lock by polling, never blocking in-statement.

A blocking ``pg_advisory_lock()`` holds a snapshot open on the waiting backend for
its whole wait, which deadlocks a concurrent-index migration run by the lock holder
(see 0007/0009 and the command docstring). These tests pin the fix: the command uses
the non-blocking ``pg_try_advisory_lock`` and retries with a sleep, holding no
snapshot between attempts.
"""

from unittest.mock import MagicMock

import project.management.commands.locked_init as locked_init


def _conn_returning(grant_sequence):
    """A fake psycopg connection whose pg_try_advisory_lock yields grant_sequence."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.side_effect = [[g] for g in grant_sequence]
    return conn


def test_acquire_lock_uses_try_lock_and_grabs_immediately(monkeypatch):
    slept = []
    monkeypatch.setattr(locked_init.time, "sleep", lambda s: slept.append(s))
    cmd = locked_init.Command()
    conn = _conn_returning([True])

    cmd._acquire_lock(conn, poll_interval=0.01)

    assert conn.execute.call_count == 1
    sql = conn.execute.call_args_list[0].args[0]
    assert "pg_try_advisory_lock" in sql
    # Never the blocking form, which would pin a snapshot while waiting.
    assert "pg_advisory_lock(" not in sql
    assert slept == []


def test_acquire_lock_polls_until_granted(monkeypatch):
    slept = []
    monkeypatch.setattr(locked_init.time, "sleep", lambda s: slept.append(s))
    cmd = locked_init.Command()
    conn = _conn_returning([False, False, True])

    cmd._acquire_lock(conn, poll_interval=0.01)

    # Two failed attempts (each followed by a sleep), then the grant.
    assert conn.execute.call_count == 3
    assert slept == [0.01, 0.01]
    for call in conn.execute.call_args_list:
        assert "pg_try_advisory_lock" in call.args[0]
