"""Tests for the TNS export client (plan U3)."""

import io
import json
import zipfile

import httpx
import pytest

from core import tns


CSV_TEXT = (
    '"2025-01-16 00:00:00"\n'
    '"objid","name_prefix","name","ra","declination","redshift","typeid","type"\n'
    '"1001","SN","2024xyz","180.0","-30.0","0.05","3","SN Ia"\n'
    '"1002","AT","2024aaa","10.0","10.0","","",""\n'
    '"bad","","","","","","",""\n'
)


def _make_zip(csv_text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("tns_public_objects.csv", csv_text)
    return buffer.getvalue()


def test_tns_marker_header_shape():
    headers = tns.tns_marker_headers(12345, "mybot")
    marker = headers["user-agent"]
    assert marker.startswith("tns_marker")
    payload = json.loads(marker[len("tns_marker"):])
    assert payload == {"tns_id": 12345, "type": "bot", "name": "mybot"}


def test_tns_marker_bad_id_raises():
    with pytest.raises(tns.TnsClientError):
        tns.tns_marker_headers("not-an-int", "mybot")


def test_hourly_delta_filename_zero_padded():
    assert tns.hourly_delta_filename(9) == "tns_public_objects_09.csv.zip"


def test_parse_skips_preamble_and_bad_rows():
    records = tns.parse_objects_csv(_make_zip(CSV_TEXT))
    # The preamble line and the malformed row are skipped; two good rows remain.
    assert [r.objid for r in records] == [1001, 1002]
    first = records[0]
    assert first.name == "2024xyz"
    assert first.name_prefix == "SN"
    assert first.type == "SN Ia"
    assert first.redshift == pytest.approx(0.05)


def test_parse_empty_classification_and_redshift_become_none():
    records = tns.parse_objects_csv(_make_zip(CSV_TEXT))
    second = records[1]  # the AT row with empty type/redshift
    assert second.type is None
    assert second.redshift is None
    assert second.name_prefix == "AT"


def test_parse_bad_zip_raises():
    with pytest.raises(tns.TnsClientError):
        tns.parse_objects_csv(b"not a zip")


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_downloads_and_parses():
    zip_bytes = _make_zip(CSV_TEXT)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("tns_marker")
        assert b"api_key=secret-key" in request.content
        return httpx.Response(200, content=zip_bytes)

    records = tns.fetch_object_records(
        base_url="https://tns.example/system/files/tns_public_objects/",
        filename=tns.FULL_OBJECTS_FILENAME,
        bot_id=1,
        bot_name="bot",
        api_key="secret-key",
        client=_client(handler),
    )
    assert [r.objid for r in records] == [1001, 1002]


def test_fetch_retries_transient_then_succeeds():
    zip_bytes = _make_zip(CSV_TEXT)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("simulated transient", request=request)
        return httpx.Response(200, content=zip_bytes)

    records = tns.fetch_object_records(
        base_url="https://tns.example/x/",
        filename=tns.FULL_OBJECTS_FILENAME,
        bot_id=1,
        bot_name="bot",
        api_key="k",
        client=_client(handler),
        retries=3,
        backoff_base=0,
    )
    assert calls["n"] == 2
    assert len(records) == 2


def test_fetch_4xx_does_not_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(tns.TnsClientError):
        tns.fetch_object_records(
            base_url="https://tns.example/x/",
            filename=tns.FULL_OBJECTS_FILENAME,
            bot_id=1,
            bot_name="bot",
            api_key="bad",
            client=_client(handler),
            retries=3,
            backoff_base=0,
        )
    assert calls["n"] == 1  # a 4xx is a hard failure, not retried


def test_fetch_5xx_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    with pytest.raises(tns.TnsClientError):
        tns.fetch_object_records(
            base_url="https://tns.example/x/",
            filename=tns.FULL_OBJECTS_FILENAME,
            bot_id=1,
            bot_name="bot",
            api_key="k",
            client=_client(handler),
            retries=3,
            backoff_base=0,
        )
    assert calls["n"] == 3  # retried up to the limit


def test_api_key_absent_from_logs(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    with pytest.raises(tns.TnsClientError):
        tns.fetch_object_records(
            base_url="https://tns.example/x/",
            filename=tns.FULL_OBJECTS_FILENAME,
            bot_id=1,
            bot_name="bot",
            api_key="super-secret-key",
            client=_client(handler),
            retries=2,
            backoff_base=0,
        )
    assert "super-secret-key" not in caplog.text
