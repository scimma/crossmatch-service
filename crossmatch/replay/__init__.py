"""Crossmatch replay: validate a stack change by re-running historical alerts.

A sample of historical alerts is exported from PROD (``sample``), replayed on DEV
through the compute step production uses (``snapshot``), and two snapshots are
compared into a grouped difference report (``compare``). See
``docs/runbooks/crossmatch-replay.md``.
"""
