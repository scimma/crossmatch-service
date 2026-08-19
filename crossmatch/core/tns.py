"""Client for TNS (Transient Name Service) public-object bulk exports.

Downloads and parses TNS's authenticated CSV exports — the daily full file
(``tns_public_objects.csv.zip``) and the hourly delta files
(``tns_public_objects_HH.csv.zip``) — so the refresh task can maintain a local
snapshot without ever calling TNS per alert.

This is the app's first outbound third-party HTTP integration. The download is
wrapped in a *narrow* transient-retry (only network/timeout/5xx, so a 4xx bad
key or a Celery control-flow exception is never misclassified as retryable), and
the ``api_key`` / ``tns_marker`` credentials are never written to logs.
"""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from dataclasses import dataclass

import httpx

from core.log import get_logger

logger = get_logger(__name__)

FULL_OBJECTS_FILENAME = "tns_public_objects.csv.zip"


class TnsClientError(Exception):
    """A non-retryable TNS download or parse failure.

    Raised for bad credentials, a 4xx response, an exhausted transient-retry, or
    a corrupt/unparseable archive. The refresh task catches this and leaves the
    prior snapshot intact (fail-soft) rather than propagating into Celery.
    """


@dataclass(frozen=True)
class TnsObjectRecord:
    """One parsed TNS object row, coerced to JSON-native Python scalars."""

    objid: int
    name: str
    name_prefix: str | None
    ra_deg: float
    dec_deg: float
    type: str | None
    redshift: float | None


def hourly_delta_filename(hour: int) -> str:
    """Return the TNS hourly-delta filename for a UT hour.

    Args:
        hour: UT hour, 0-23.

    Returns:
        The zero-padded delta filename, e.g. ``tns_public_objects_09.csv.zip``.
    """
    return f"tns_public_objects_{hour:02d}.csv.zip"


def tns_marker_headers(bot_id: str | int, bot_name: str) -> dict[str, str]:
    """Build the mandatory ``tns_marker`` User-Agent header for programmatic access.

    Args:
        bot_id: The numeric TNS bot id.
        bot_name: The TNS bot name.

    Returns:
        A one-key dict suitable as request headers.

    Raises:
        TnsClientError: If ``bot_id`` is not an integer (a misconfigured secret).
    """
    try:
        marker = json.dumps(
            {"tns_id": int(bot_id), "type": "bot", "name": str(bot_name)},
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise TnsClientError("TNS bot id must be an integer") from exc
    return {"user-agent": f"tns_marker{marker}"}


def _none_if_empty(value: str | None) -> str | None:
    """Return ``None`` for an empty/whitespace field, else the stripped string."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _coerce_float(value: str | None) -> float | None:
    """Parse a CSV float field, returning ``None`` for empty/unparseable input."""
    text = _none_if_empty(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _record_from_row(row: dict[str, str]) -> TnsObjectRecord | None:
    """Build a :class:`TnsObjectRecord` from one CSV row, or ``None`` if unusable.

    A row is unusable (and skipped, not fatal) when it lacks a parseable
    ``objid`` or valid ``ra``/``declination`` — the three fields the association
    step cannot work without.
    """
    objid_text = _none_if_empty(row.get("objid"))
    ra = _coerce_float(row.get("ra"))
    dec = _coerce_float(row.get("declination"))
    name = _none_if_empty(row.get("name"))
    if objid_text is None or ra is None or dec is None or name is None:
        return None
    try:
        objid = int(objid_text)
    except ValueError:
        return None
    return TnsObjectRecord(
        objid=objid,
        name=name,
        name_prefix=_none_if_empty(row.get("name_prefix")),
        ra_deg=ra,
        dec_deg=dec,
        type=_none_if_empty(row.get("type")),
        redshift=_coerce_float(row.get("redshift")),
    )


def parse_objects_csv(zip_bytes: bytes) -> list[TnsObjectRecord]:
    """Parse a TNS public-objects zip archive into object records.

    The archive holds a single CSV whose first line is a metadata/timestamp
    preamble, followed by the column header and rows. This finds the real header
    (the first line naming ``objid``) so the preamble is skipped robustly, and
    drops rows missing objid / coordinates / name rather than failing the batch.

    Args:
        zip_bytes: The raw ``.csv.zip`` payload.

    Returns:
        The list of parseable object records.

    Raises:
        TnsClientError: If the archive holds no CSV or has no recognizable header.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            csv_names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise TnsClientError("TNS archive contained no CSV file")
            text = archive.read(csv_names[0]).decode("utf-8", errors="replace")
    except zipfile.BadZipFile as exc:
        raise TnsClientError("TNS download was not a valid zip archive") from exc

    lines = text.splitlines()
    header_index = next(
        (i for i, line in enumerate(lines) if "objid" in line and "name" in line),
        None,
    )
    if header_index is None:
        raise TnsClientError("TNS CSV had no recognizable header row")

    reader = csv.DictReader(lines[header_index:])
    records: list[TnsObjectRecord] = []
    skipped = 0
    for row in reader:
        record = _record_from_row(row)
        if record is None:
            skipped += 1
            continue
        records.append(record)
    if skipped:
        logger.debug("tns_rows_skipped", skipped=skipped, kept=len(records))
    return records


def fetch_object_records(
    *,
    base_url: str,
    filename: str,
    bot_id: str | int,
    bot_name: str,
    api_key: str,
    client: httpx.Client | None = None,
    timeout: float = 120.0,
    retries: int = 3,
    backoff_base: float = 2.0,
) -> list[TnsObjectRecord]:
    """Download and parse one TNS export file into object records.

    POSTs to ``base_url + filename`` with the ``tns_marker`` User-Agent and the
    ``api_key`` form field, retrying only transient failures (network, timeout,
    5xx). A 4xx is treated as a hard, non-retryable error.

    Args:
        base_url: The TNS public-objects base URL (trailing slash tolerated).
        filename: The export filename (full file or an hourly delta).
        bot_id: TNS bot id.
        bot_name: TNS bot name.
        api_key: TNS bot API key (never logged).
        client: An optional pre-built ``httpx.Client`` (tests inject a mock
            transport); a default client is created and closed when omitted.
        timeout: Per-request timeout in seconds.
        retries: Total download attempts before giving up.
        backoff_base: Linear backoff base (seconds) between transient retries.

    Returns:
        The parsed object records.

    Raises:
        TnsClientError: On a 4xx, an exhausted transient-retry, or a bad archive.
    """
    url = base_url.rstrip("/") + "/" + filename
    headers = tns_marker_headers(bot_id, bot_name)
    owns_client = client is None
    active = client or httpx.Client()
    try:
        content = _download(
            active,
            url,
            headers=headers,
            api_key=api_key,
            timeout=timeout,
            retries=retries,
            backoff_base=backoff_base,
        )
    finally:
        if owns_client:
            active.close()
    return parse_objects_csv(content)


def _download(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    api_key: str,
    timeout: float,
    retries: int,
    backoff_base: float,
) -> bytes:
    """POST for the export bytes with a narrow transient-retry.

    Note: request data carrying ``api_key`` and the ``tns_marker`` header are
    never included in log output — only the URL, status, and attempt count.
    """
    for attempt in range(1, retries + 1):
        try:
            response = client.post(
                url, headers=headers, data={"api_key": api_key}, timeout=timeout
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            logger.warning(
                "tns_download_transient_error", url=url, attempt=attempt, error=str(exc)
            )
            if attempt < retries:
                time.sleep(backoff_base * attempt)
                continue
            raise TnsClientError(
                f"TNS download failed after {retries} attempts"
            ) from exc

        if response.status_code >= 500:
            logger.warning(
                "tns_download_server_error",
                url=url,
                status=response.status_code,
                attempt=attempt,
            )
            if attempt < retries:
                time.sleep(backoff_base * attempt)
                continue
            raise TnsClientError(
                f"TNS download failed with status {response.status_code}"
            )
        if response.status_code != 200:
            # 4xx (e.g. a bad api_key) is a hard failure — do not retry.
            raise TnsClientError(
                f"TNS download returned status {response.status_code}"
            )
        return response.content

    # Unreachable: the loop either returns or raises.
    raise TnsClientError("TNS download exhausted retries")
