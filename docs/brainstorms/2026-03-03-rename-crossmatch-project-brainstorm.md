---
date: 2026-03-03
topic: rename crossmatch_project to crossmatch
branch: refactor/align-skeleton-to-design
---

# Brainstorm: Rename `crossmatch_project/` → `crossmatch/`

## What We're Building

Rename the Django settings package from `crossmatch_project/` to `crossmatch/`, creating
a nested `crossmatch/crossmatch/` structure (source root contains inner settings package
of the same name). This is the standard Django convention where the settings package name
matches the project name.

## Why This Approach

**Motivation:** `crossmatch_project` is verbose. The nested naming yields:
- `DJANGO_SETTINGS_MODULE=crossmatch.settings` — cleaner and conventional
- `celery -A crossmatch worker` — unambiguous, matches the Celery app name already used
- `INSTALLED_APPS` entry `'crossmatch'` — consistent with the project identity

**Naming conflict analysis:** The outer source root is also named `crossmatch/` but is NOT
a Python package (no `__init__.py` at the repo root level above it). Python resolves imports
with the source root on `sys.path`, so `import crossmatch` finds the inner
`crossmatch/crossmatch/__init__.py` correctly. No ambiguity in practice.

## Key Decisions

1. **Rename approach**: `git mv crossmatch/crossmatch_project crossmatch/crossmatch`
2. **Settings module string**: `crossmatch_project.settings` → `crossmatch.settings`
3. **Celery `-A` flag**: `-A crossmatch_project` → `-A crossmatch` (entrypoints)
4. **INSTALLED_APPS**: `'crossmatch_project'` → `'crossmatch'`
5. **Management commands**: stay under `crossmatch/management/commands/` (no extra moves)

## Files to Change

| File | Change |
|---|---|
| `crossmatch/manage.py` | `DJANGO_SETTINGS_MODULE` value |
| `crossmatch/crossmatch/celery.py` | `DJANGO_SETTINGS_MODULE` value + module import path if needed |
| `crossmatch/crossmatch/settings.py` | `INSTALLED_APPS` entry |
| `crossmatch/entrypoints/run_celery_worker.sh` | `-A crossmatch_project` → `-A crossmatch` |
| `crossmatch/entrypoints/run_celery_beat.sh` | `-A crossmatch_project` → `-A crossmatch` |

Total: 5 files, 8 string occurrences (all mechanical find-and-replace after the `git mv`).

## Open Questions

- **IDE tooling**: Some editors (e.g. PyCharm) may need the Django project root reconfigured
  after the rename. No code impact, just a developer setup note.
- **No other unresolved questions** — the rename is mechanical and fully self-contained within
  the current branch.
