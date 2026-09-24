"""File-format identifiers for replay snapshots, kept free of Django imports.

The comparison step reads snapshots anywhere (including a laptop without the
app's database settings), so it must not import the snapshot builder.
"""

SNAPSHOT_KIND = "crossmatch-replay-snapshot"
SNAPSHOT_FORMAT_VERSION = 1
