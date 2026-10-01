"""Shared pytest fixtures for the crossmatch test suite."""

from typing import Any, Callable

import pytest

from tests.factories import make_alert_with_notifications


@pytest.fixture(autouse=True)
def _full_sky_catalog_coverage(monkeypatch):
    """Stub the crossmatch task's coverage-map seam with a full-sky MOC.

    The footprint test (KTD6) reads each catalog's HATS coverage map through
    ``tasks.crossmatch.catalog_moc``, which would open the real catalog. Tests
    that mock ``crossmatch_alerts`` get a whole-sky map so every valid position
    is inside; tests of the footprint itself patch the seam again.
    """
    from mocpy import MOC

    import tasks.crossmatch as crossmatch_mod

    full_sky = MOC.from_string('0/0-11')
    monkeypatch.setattr(crossmatch_mod, 'catalog_moc', lambda cfg: full_sky)


@pytest.fixture
def make_alert():
    """Builder fixture: make_alert(status, [Notification.State, ...]) -> (alert, notifications).

    Tests using this still need the django_db marker (it writes rows).
    """
    return make_alert_with_notifications


@pytest.fixture
def openapi_validate(client) -> Callable[[str, int, Any], None]:
    """Validate a response body against the served OpenAPI document (KTD14).

    Fetches ``/openapi.json`` through the test client once per test, so the
    check runs against the document the service actually serves under the
    test's settings, not a copy built separately.

    Returns:
        ``validate(operation_id, status, body)``, which raises
        ``AssertionError`` if the operation or status is not documented (or
        documents no JSON body) and ``jsonschema.ValidationError`` if ``body``
        does not conform to the documented schema.
    """
    import jsonschema

    resp = client.get('/openapi.json')
    assert resp.status_code == 200, 'openapi.json is not served'
    doc = resp.json()

    def validate(operation_id: str, status: int, body: Any) -> None:
        for path, item in doc['paths'].items():
            for method, operation in item.items():
                if isinstance(operation, dict) and operation.get('operationId') == operation_id:
                    break
            else:
                continue
            break
        else:
            raise AssertionError(f'operation {operation_id!r} is not in the OpenAPI document')

        responses = operation.get('responses', {})
        assert str(status) in responses, (
            f'status {status} is not documented for {operation_id!r} '
            f'({method.upper()} {path}); documented: {sorted(responses)}'
        )
        content = responses[str(status)].get('content', {})
        assert 'application/json' in content, (
            f'{operation_id!r} {status} documents no application/json body'
        )
        pointer = '/'.join(
            ['#', 'paths', path.replace('~', '~0').replace('/', '~1'), method,
             'responses', str(status), 'content', 'application~1json', 'schema']
        )
        # Validate from the document root so the schema's $refs into
        # #/components resolve; the OpenAPI keywords alongside the $ref are
        # unknown to JSON Schema 2020-12 and ignored.
        jsonschema.Draft202012Validator({**doc, '$ref': pointer}).validate(body)

    return validate
