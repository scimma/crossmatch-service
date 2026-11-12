"""Encode Babamul fixture alerts the way Babamul's Kafka topics carry them.

Each Babamul message value is an Avro Object Container File with its schema
embedded (one record per message). The schema here is a minimal,
schema-faithful subset of Babamul's LSST alert: enough fields to exercise the
consumer's decode and the normalizer, plus the cutout bytes the payload must
drop.
"""

import copy
import io
import json
from pathlib import Path

import fastavro

FIXTURE = Path(__file__).parent / 'fixtures' / 'brokers' / 'babamul_alert.json'


def _nullable(avro_type):
    return ['null', avro_type]


_PHOTOMETRY = {
    'type': 'record',
    'name': 'Photometry',
    'fields': [
        {'name': 'jd', 'type': 'double'},
        {'name': 'psfFlux', 'type': _nullable('double'), 'default': None},
        {'name': 'psfFluxErr', 'type': 'double'},
        {'name': 'band', 'type': 'string'},
        {'name': 'ra', 'type': _nullable('double'), 'default': None},
        {'name': 'dec', 'type': _nullable('double'), 'default': None},
    ],
}

SCHEMA = fastavro.parse_schema(
    {
        'type': 'record',
        'name': 'BabamulLsstAlert',
        'fields': [
            {'name': 'candid', 'type': 'long'},
            {'name': 'objectId', 'type': 'string'},
            {
                'name': 'candidate',
                'type': {
                    'type': 'record',
                    'name': 'LsstCandidate',
                    'fields': [
                        {'name': 'diaSourceId', 'type': 'long'},
                        {'name': 'visit', 'type': 'long'},
                        {'name': 'detector', 'type': 'int'},
                        {
                            'name': 'diaObjectId',
                            'type': _nullable('long'),
                            'default': None,
                        },
                        {
                            'name': 'ssObjectId',
                            'type': _nullable('long'),
                            'default': None,
                        },
                        {'name': 'midpointMjdTai', 'type': 'double'},
                        {'name': 'ra', 'type': _nullable('double'), 'default': None},
                        {'name': 'dec', 'type': _nullable('double'), 'default': None},
                        {
                            'name': 'reliability',
                            'type': _nullable('double'),
                            'default': None,
                        },
                        {'name': 'jd', 'type': 'double'},
                        {
                            'name': 'jdstarthist',
                            'type': _nullable('double'),
                            'default': None,
                        },
                        {'name': 'band', 'type': _nullable('string'), 'default': None},
                    ],
                },
            },
            {
                'name': 'prv_candidates',
                'type': _nullable({'type': 'array', 'items': _PHOTOMETRY}),
                'default': None,
            },
            {
                'name': 'fp_hists',
                'type': _nullable({'type': 'array', 'items': 'Photometry'}),
                'default': None,
            },
            {
                'name': 'survey_matches',
                'type': _nullable(
                    {
                        'type': 'record',
                        'name': 'SurveyMatches',
                        'fields': [
                            {
                                'name': 'ztf',
                                'type': _nullable(
                                    {
                                        'type': 'record',
                                        'name': 'ZtfMatch',
                                        'fields': [
                                            {'name': 'objectId', 'type': 'string'},
                                            {'name': 'ra', 'type': 'double'},
                                            {'name': 'dec', 'type': 'double'},
                                            {
                                                'name': 'prv_candidates',
                                                'type': {
                                                    'type': 'array',
                                                    'items': 'Photometry',
                                                },
                                            },
                                        ],
                                    }
                                ),
                                'default': None,
                            }
                        ],
                    }
                ),
                'default': None,
            },
            {'name': 'cutoutScience', 'type': _nullable('bytes'), 'default': None},
        ],
    }
)


def fixture_record(**candidate_overrides):
    """The fixture alert as a decoded dict, with candidate fields overridden."""
    record = copy.deepcopy(json.loads(FIXTURE.read_text()))
    record['candidate'].update(candidate_overrides)
    record['cutoutScience'] = b'\x89PNG-not-really'
    return record


def encode(record):
    """Encode one alert as an Avro Object Container File, as Babamul sends it."""
    buffer = io.BytesIO()
    fastavro.writer(buffer, SCHEMA, [record])
    return buffer.getvalue()
