"""Dump the parquet schema for each configured crossmatch catalog.

Reads the HATS `_common_metadata` file from every catalog's HATS
directory and writes a per-catalog markdown reference under
``docs/references/``. The output is meant to confirm column names in
``docs/brainstorms/2026-04-27-payload-columns-by-keyword-brainstorm.md``
before we commit to a payload spec.

Run from the repo root::

    python -m venv .venv && source .venv/bin/activate
    pip install pyarrow 'fsspec[http]' s3fs
    python scripts/dump_catalog_columns.py

Each catalog URL can be overridden via the same env vars used in
``crossmatch/project/settings.py`` (``GAIA_HATS_URL`` etc.). For
networks that cannot reach AWS S3, the data.lsdb.io mirror at
``https://data.lsdb.io/hats/gaia_dr3/gaia`` is schema-identical to
the production stpubdata S3 source.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import fsspec
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "docs" / "references"

CATALOGS: dict[str, str] = {
    "gaia_dr3": os.environ.get(
        "GAIA_HATS_URL", "s3://stpubdata/gaia/gaia_dr3/public/hats"
    ),
    "des_y6_gold": os.environ.get(
        "DES_HATS_URL", "https://data.lsdb.io/hats/des/des_y6_gold"
    ),
    "delve_dr3_gold": os.environ.get(
        "DELVE_HATS_URL", "https://data.lsdb.io/hats/delve/delve_dr3_gold"
    ),
    "skymapper_dr4": os.environ.get(
        "SKYMAPPER_HATS_URL", "https://data.lsdb.io/hats/skymapper_dr4/catalog"
    ),
}


def _open_fs(url: str):
    """Return (fs, path) with anonymous credentials for public S3."""
    if url.startswith("s3://"):
        return fsspec.core.url_to_fs(url, anon=True)
    return fsspec.core.url_to_fs(url)


def resolve_catalog_root(fs, path: str) -> str:
    """If `path` is a HATS collection, descend to the default catalog."""
    collection_props = f"{path.rstrip('/')}/collection.properties"
    if not fs.exists(collection_props):
        return path

    text = fs.cat(collection_props).decode("utf-8")
    default = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("default_catalog="):
            default = line.split("=", 1)[1].strip()
            break
    if default is not None:
        return f"{path.rstrip('/')}/{default}"

    for entry in fs.ls(path, detail=True):
        name = entry["name"].rstrip("/").rsplit("/", 1)[-1]
        if entry["type"] == "directory" and not name.startswith("_"):
            return f"{path.rstrip('/')}/{name}"
    raise RuntimeError(f"cannot resolve collection at {path}")


def load_schema(url: str):
    fs, path = _open_fs(url)
    catalog_root = resolve_catalog_root(fs, path)
    meta_path = f"{catalog_root.rstrip('/')}/dataset/_common_metadata"
    with fs.open(meta_path, "rb") as src:
        buf = io.BytesIO(src.read())
    return pq.read_schema(buf), catalog_root


def write_markdown(name: str, url: str, resolved_root: str, schema) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / f"{name}-columns.md"
    lines = [
        f"# {name} — column reference",
        "",
        f"- Configured URL: `{url}`",
        f"- Resolved catalog root: `{resolved_root}`",
        f"- Total columns: **{len(schema)}**",
        "",
        "| # | Column | Arrow dtype |",
        "| - | ------ | ----------- |",
    ]
    for i, field in enumerate(schema, start=1):
        lines.append(f"| {i} | `{field.name}` | `{field.type}` |")
    out.write_text("\n".join(lines) + "\n")
    return out


def main() -> int:
    failures: list[tuple[str, str, Exception]] = []
    for name, url in CATALOGS.items():
        try:
            schema, resolved = load_schema(url)
            out = write_markdown(name, url, resolved, schema)
            print(
                f"[ok]   {name}: {len(schema)} columns -> "
                f"{out.relative_to(REPO_ROOT)}"
            )
        except Exception as exc:
            failures.append((name, url, exc))
            print(f"[fail] {name} ({url}): {exc}", file=sys.stderr)

    if failures:
        print("\nFailures:", file=sys.stderr)
        for name, url, exc in failures:
            print(f"  - {name} <{url}>: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
