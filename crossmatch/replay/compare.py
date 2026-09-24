"""Compare two replay snapshots into a grouped difference report.

Every output difference between the baseline and candidate is listed; none is
suppressed or accepted automatically. Differences are grouped by kind, catalog,
and field so one explanation in the upgrade PR can cover a whole group, and the
run-context differences -- including anything that makes the two snapshots
incomparable -- are reported before the output differences.

This module has no Django dependency; it reads snapshot files anywhere.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from replay.formats import SNAPSHOT_FORMAT_VERSION, SNAPSHOT_KIND

# Output difference kinds.
MATCH_ONLY_IN_BASELINE = "match present only in baseline"
MATCH_ONLY_IN_CANDIDATE = "match present only in candidate"
SOURCE_CHANGED = "matched source changed"
VALUE_CHANGED = "value changed"
TYPE_CHANGED = "type or null representation changed"
TNS_CHANGED = "TNS block changed"
TNS_DRIFT = "TNS block changed (TNS snapshot drift)"

_KIND_ORDER = (
    MATCH_ONLY_IN_BASELINE,
    MATCH_ONLY_IN_CANDIDATE,
    SOURCE_CHANGED,
    TYPE_CHANGED,
    VALUE_CHANGED,
    TNS_CHANGED,
    TNS_DRIFT,
)

#: Published-payload keys compared as run context, not output (R11).
_CONTEXT_ONLY_PAYLOAD_KEYS = frozenset({"tns_snapshot_epoch"})
_TNS_PAYLOAD_KEYS = frozenset({"tns", "tns_checked"})

_MISSING = object()
_MAX_EXAMPLES = 3


class SnapshotFormatError(ValueError):
    """A file is not a replay snapshot of a supported format version."""


@dataclass
class ContextDifference:
    """A run-context value that differs between the snapshots."""

    field: str
    baseline: object
    candidate: object


@dataclass
class DifferenceGroup:
    """Output differences sharing a kind, catalog, and field."""

    kind: str
    catalog: str
    field: str
    count: int = 0
    max_abs_diff: float | None = None
    max_rel_diff: float | None = None
    examples: list = field(default_factory=list)

    def add(self, dia_object_id, baseline, candidate) -> None:
        """Record one difference, keeping the largest numeric change."""
        self.count += 1
        if len(self.examples) < _MAX_EXAMPLES:
            self.examples.append((dia_object_id, baseline, candidate))
        if _is_number(baseline) and _is_number(candidate):
            abs_diff = abs(candidate - baseline)
            rel_diff = abs_diff / abs(baseline) if baseline else math.inf
            if self.max_abs_diff is None or abs_diff > self.max_abs_diff:
                self.max_abs_diff = abs_diff
            if self.max_rel_diff is None or rel_diff > self.max_rel_diff:
                self.max_rel_diff = rel_diff


@dataclass
class Comparison:
    """The result of comparing a baseline snapshot with a candidate.

    Attributes:
        flags: Reasons the snapshots are not comparable, reported first.
        context_differences: Every run-context value that differs.
        groups: Grouped output differences, in a stable order.
        matches_compared: Distinct (diaObjectId, catalog) keys across both.
    """

    flags: list = field(default_factory=list)
    context_differences: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    matches_compared: int = 0

    @property
    def difference_count(self) -> int:
        """Total output differences across all groups."""
        return sum(group.count for group in self.groups)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_snapshot(path: Path | str) -> dict:
    """Load a snapshot file, checking its kind and format version.

    Args:
        path: The snapshot JSON file.

    Returns:
        The parsed snapshot.

    Raises:
        SnapshotFormatError: The file is not a supported replay snapshot.
    """
    data = json.loads(Path(path).read_text())
    if data.get("kind") != SNAPSHOT_KIND:
        raise SnapshotFormatError(f"{path}: not a replay snapshot")
    if data.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        raise SnapshotFormatError(
            f'{path}: unsupported format_version {data.get("format_version")!r}'
        )
    return data


# --- Run context -------------------------------------------------------------


def _flatten(value, prefix: str) -> dict:
    """Flatten nested dicts into ``{'a.b.c': leaf}``."""
    if isinstance(value, dict):
        flat = {}
        for key, child in value.items():
            flat.update(_flatten(child, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    return {prefix: value}


def _version_view(versions: dict) -> dict:
    """Versions keyed by role and package; workers collapse across addresses.

    Worker addresses change between runs (pod IPs), so each worker package is
    reported as the sorted set of versions seen across all workers.
    """
    view = {}
    for role in ("client", "scheduler"):
        for pkg, ver in (versions.get(role) or {}).items():
            view[f"versions.{role}.{pkg}"] = ver
    worker_versions: dict = {}
    for packages in (versions.get("workers") or {}).values():
        for pkg, ver in (packages or {}).items():
            worker_versions.setdefault(pkg, set()).add(str(ver))
    for pkg, vers in worker_versions.items():
        view[f"versions.workers.{pkg}"] = ", ".join(sorted(vers))
    return view


def _context_view(ctx: dict) -> dict:
    view = {}
    view.update(_version_view(ctx.get("versions") or {}))
    view["image_tag"] = ctx.get("image_tag")
    view["sample.digest"] = (ctx.get("sample") or {}).get("digest")
    view.update(_flatten(ctx.get("crossmatch") or {}, "crossmatch"))
    view.update(_flatten(ctx.get("catalog_outcomes") or {}, "catalog_outcomes"))
    view.update(_flatten(ctx.get("catalog_identity") or {}, "catalog_identity"))
    tns = ctx.get("tns") or {}
    for key in ("current", "epoch", "content_identity"):
        view[f"tns.{key}"] = tns.get(key)
    return view


def _comparability_flags(base_ctx: dict, cand_ctx: dict) -> list:
    flags = []
    base_x = base_ctx.get("crossmatch") or {}
    cand_x = cand_ctx.get("crossmatch") or {}
    if (base_ctx.get("sample") or {}).get("digest") != (
        cand_ctx.get("sample") or {}
    ).get("digest"):
        flags.append("The snapshots replayed different samples.")
    if base_x.get("radius_arcsec") != cand_x.get("radius_arcsec"):
        flags.append(
            f"Crossmatch radius differs ({base_x.get('radius_arcsec')} vs "
            f"{cand_x.get('radius_arcsec')} arcsec)."
        )
    if base_x.get("catalogs") != cand_x.get("catalogs"):
        flags.append(
            "Catalog configuration differs (catalog list, URL, columns, or "
            "payload columns)."
        )
    for key in ("tns_match_radius_arcsec", "tns_object_url_template"):
        if base_x.get(key) != cand_x.get(key):
            flags.append(f"TNS setting {key} differs.")
    for label, ctx in (("baseline", base_ctx), ("candidate", cand_ctx)):
        for name, outcome in (ctx.get("catalog_outcomes") or {}).items():
            if outcome == "skipped":
                flags.append(
                    f"Catalog {name} was skipped after a read failure in the "
                    f"{label}; re-run that replay."
                )
        if not (ctx.get("tns") or {}).get("current"):
            flags.append(
                f"The TNS snapshot was not current in the {label}, so TNS "
                f"enrichment was not exercised."
            )
    if base_ctx.get("catalog_identity") != cand_ctx.get("catalog_identity"):
        flags.append(
            "A hosted catalog build differs between the runs (see "
            "catalog_identity below); its differences may not come from the "
            "stack change."
        )
    return flags


# --- Output ------------------------------------------------------------------


def _leaf_differences(base, cand, path: str):
    """Yield ``(path, baseline, candidate)`` for every differing leaf."""
    if isinstance(base, dict) and isinstance(cand, dict):
        for key in sorted(set(base) | set(cand), key=str):
            child = f"{path}.{key}" if path else str(key)
            yield from _leaf_differences(
                base.get(key, _MISSING), cand.get(key, _MISSING), child
            )
        return
    if type(base) is not type(cand):
        yield path, base, cand
    elif base != cand:
        yield path, base, cand


def _value_kind(base, cand) -> str:
    if base is _MISSING or cand is _MISSING or base is None or cand is None:
        return TYPE_CHANGED
    if type(base) is not type(cand):
        return TYPE_CHANGED
    return VALUE_CHANGED


def _shown(value):
    return "missing" if value is _MISSING else value


def compare_snapshots(baseline: dict, candidate: dict) -> Comparison:
    """Compare a baseline snapshot with a candidate.

    Args:
        baseline: The snapshot taken before the stack change.
        candidate: The snapshot taken after it.

    Returns:
        The :class:`Comparison`.
    """
    base_ctx = baseline.get("run_context") or {}
    cand_ctx = candidate.get("run_context") or {}
    result = Comparison(flags=_comparability_flags(base_ctx, cand_ctx))

    base_view = _context_view(base_ctx)
    cand_view = _context_view(cand_ctx)
    for key in sorted(set(base_view) | set(cand_view)):
        if base_view.get(key) != cand_view.get(key):
            result.context_differences.append(
                ContextDifference(key, base_view.get(key), cand_view.get(key))
            )

    base_fp = (base_ctx.get("tns") or {}).get("fingerprints") or {}
    cand_fp = (cand_ctx.get("tns") or {}).get("fingerprints") or {}

    def _index(snapshot):
        return {
            (m["dia_object_id"], m["catalog"]): m for m in snapshot.get("matches", [])
        }

    base_matches = _index(baseline)
    cand_matches = _index(candidate)
    groups: dict = {}

    def _group(kind, catalog, field_name):
        key = (kind, catalog, field_name)
        if key not in groups:
            groups[key] = DifferenceGroup(kind, catalog, field_name)
        return groups[key]

    keys = sorted(set(base_matches) | set(cand_matches))
    result.matches_compared = len(keys)
    for dia_id, catalog in keys:
        base = base_matches.get((dia_id, catalog))
        cand = cand_matches.get((dia_id, catalog))
        if cand is None:
            _group(MATCH_ONLY_IN_BASELINE, catalog, "match").add(
                dia_id, base["source_id"], "missing"
            )
            continue
        if base is None:
            _group(MATCH_ONLY_IN_CANDIDATE, catalog, "match").add(
                dia_id, "missing", cand["source_id"]
            )
            continue
        if base["source_id"] != cand["source_id"]:
            _group(SOURCE_CHANGED, catalog, "source_id").add(
                dia_id, base["source_id"], cand["source_id"]
            )
            continue
        drifted = base_fp.get(str(dia_id)) != cand_fp.get(str(dia_id))
        for path, b_val, c_val in _leaf_differences(
            base.get("published_payload") or {},
            cand.get("published_payload") or {},
            "",
        ):
            top = path.split(".", 1)[0]
            if top in _CONTEXT_ONLY_PAYLOAD_KEYS:
                continue
            if top in _TNS_PAYLOAD_KEYS:
                kind = TNS_DRIFT if drifted else TNS_CHANGED
            else:
                kind = _value_kind(b_val, c_val)
            _group(kind, catalog, path).add(dia_id, _shown(b_val), _shown(c_val))

    result.groups = sorted(
        groups.values(),
        key=lambda g: (_KIND_ORDER.index(g.kind), g.catalog, g.field),
    )
    return result


# --- Report ------------------------------------------------------------------


def _cell(value) -> str:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text.replace("|", "\\|")


def render_report(result: Comparison, baseline_path: str, candidate_path: str) -> str:
    """Render the comparison as a Markdown report for the upgrade PR.

    Args:
        result: The comparison.
        baseline_path: Shown as the baseline's name.
        candidate_path: Shown as the candidate's name.

    Returns:
        The Markdown text.
    """
    lines = [
        "# Crossmatch replay comparison",
        "",
        f"- Baseline: `{baseline_path}`",
        f"- Candidate: `{candidate_path}`",
        f"- Matches compared: {result.matches_compared}",
        f"- Output differences: {result.difference_count} in "
        f"{len(result.groups)} groups",
        "",
        "## Comparability",
        "",
    ]
    if result.flags:
        lines += [f"- {flag}" for flag in result.flags]
    else:
        lines.append("No comparability problems.")

    lines += ["", "## Run context differences", ""]
    if result.context_differences:
        lines += ["| Field | Baseline | Candidate |", "|---|---|---|"]
        lines += [
            f"| {d.field} | {_cell(d.baseline)} | {_cell(d.candidate)} |"
            for d in result.context_differences
        ]
    else:
        lines.append("None.")

    lines += ["", "## Output differences", ""]
    if not result.groups:
        lines.append("No output differences.")
    for group in result.groups:
        lines += [
            f"### {group.kind}: {group.catalog} / {group.field} ({group.count})",
            "",
        ]
        if group.max_abs_diff is not None:
            lines += [
                f"Largest change: {group.max_abs_diff:.6g} absolute, "
                f"{group.max_rel_diff:.3g} relative.",
                "",
            ]
        lines += ["| diaObjectId | Baseline | Candidate |", "|---|---|---|"]
        lines += [
            f"| {dia_id} | {_cell(b_val)} | {_cell(c_val)} |"
            for dia_id, b_val, c_val in group.examples
        ]
        if group.count > len(group.examples):
            lines.append(f"| ... | {group.count - len(group.examples)} more | |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
