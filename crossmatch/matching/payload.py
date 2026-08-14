"""Build the catalog-specific payload from a crossmatched row.

A crossmatch result carries catalog columns under their upstream-native names
and dtypes — numpy scalars (int16/int32/int64, float32/float64, bool_) and
pandas missing values. The published payload needs lowercase keys and plain
JSON-native Python scalars, because the values are stored in a Django
``JSONField`` and published as JSON over Hopskotch (a ``JSONField`` cannot hold
numpy scalars, and ``json`` cannot serialize them).

This module is intentionally free of LSDB and Django imports so it can be
exercised in isolation; numpy and pandas are used only to recognize the scalar
types and missing-value sentinels that flow out of the crossmatch DataFrame.
"""

import numpy as np
import pandas as pd


def _to_json_scalar(value):
    """Coerce one catalog value to a JSON-native scalar.

    Missing values (``None``, NaN, NaT, ``pd.NA``) become ``None``. numpy
    integers / floats / booleans become their Python equivalents; strings pass
    through; anything else is stringified as a last resort.
    """
    # pd.isna recognizes None, float nan, np.nan, pd.NaT and pd.NA uniformly.
    # Guard to scalars first: pd.isna on an array returns an array, whose truth
    # value is ambiguous. Crossmatch rows yield scalars, so this is just safety.
    if value is None or (np.ndim(value) == 0 and pd.isna(value)):
        return None
    # bool before int: Python bool is a subclass of int, and np.bool_ is not.
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value)
    if isinstance(value, str):
        return value
    # Some other numpy scalar: unwrap to a Python object and retry once.
    item = getattr(value, "item", None)
    if callable(item):
        return _to_json_scalar(item())
    return str(value)


def build_catalog_payload(values, payload_columns):
    """Return the lowercase-keyed, JSON-native payload for one matched row.

    Args:
        values: Mapping of upstream-native column name -> raw value for the row
            (numpy scalars / pandas missing values as they come off the
            crossmatch result DataFrame).
        payload_columns: Ordered list of upstream-native column names to include
            (a catalog's configured ``payload_columns``).

    Returns:
        Dict keyed by the lowercased column name, in ``payload_columns`` order.
        Every declared column is present; a value that is missing or NaN for
        this row is ``None`` (stable key set per catalog). Keys are lowercased
        (e.g. ``WAVG_MAG_PSF_G`` -> ``wavg_mag_psf_g``); already-lowercase names
        such as SkyMapper's ``raj2000`` are unchanged, so the J2000 suffix is
        preserved.
    """
    return {
        col.lower(): _to_json_scalar(values.get(col))
        for col in payload_columns
    }


def _epoch_to_iso(epoch):
    """Render a snapshot epoch as a JSON-safe ISO-8601 string (or ``None``).

    ``_to_json_scalar`` has no datetime branch and ``json.dumps`` cannot encode a
    raw ``datetime``, so the enrichment indicator's epoch is serialized here.
    """
    if epoch is None:
        return None
    isoformat = getattr(epoch, "isoformat", None)
    return isoformat() if callable(isoformat) else str(epoch)


def _tns_block(tns):
    """Build the published ``tns`` sub-object from a match mapping.

    Args:
        tns: Mapping with ``objid``, ``name``, ``url``, ``separation_arcsec`` and
            optional ``classification`` / ``redshift`` for the associated TNS
            object.

    Returns:
        A JSON-native dict: ``objid`` (int), ``name`` (str), ``classification``
        (str or None), ``redshift`` (float or None), ``separation_arcsec``
        (float), ``url`` (str).
    """
    return {
        'objid': int(tns['objid']),
        'name': tns['name'],
        'classification': _to_json_scalar(tns.get('classification')),
        'redshift': _to_json_scalar(tns.get('redshift')),
        'separation_arcsec': float(tns['separation_arcsec']),
        'url': tns['url'],
    }


def build_published_payload(
    dia_object_id,
    source_ra_deg,
    source_dec_deg,
    catalog_name,
    catalog_source_id,
    separation_arcsec,
    catalog_payload,
    catalogs_skipped=None,
    tns=None,
    tns_checked=False,
    tns_snapshot_epoch=None,
):
    """Build the per-match payload published over Hopskotch.

    Single source of truth for the published payload shape, called by both the
    crossmatch publish path (``tasks/crossmatch.py``) and the read-model API's
    ``full`` detail level, so the two cannot drift. The ``ra``/``dec`` are the
    matched catalog-source coordinates (not the alert object's position); the
    per-catalog columns live nested under ``catalog_payload``.

    Args:
        dia_object_id: The alert's ``diaObjectId`` (coerced to int64).
        source_ra_deg: Matched catalog source right ascension in degrees.
        source_dec_deg: Matched catalog source declination in degrees.
        catalog_name: Catalog the match came from (e.g. ``gaia_dr3``).
        catalog_source_id: Source identifier in that catalog.
        separation_arcsec: Angular separation between alert and source, arcsec.
        catalog_payload: The catalog-specific payload dict (see
            :func:`build_catalog_payload`).
        catalogs_skipped: Names of catalogs skipped in this crossmatch batch
            because their reads persistently failed (``None`` / empty means the
            crossmatch covered every configured catalog). Drives ``partial``.
            The read-model API serves a stored match with no batch context, so it
            passes ``None`` here (the published Hopskotch payload carries the real
            per-batch value).
        tns: The associated TNS object as a mapping (see :func:`_tns_block`), or
            ``None`` when the alert had no TNS counterpart (or was not checked).
            Emitted under a nested ``tns`` key only when present.
        tns_checked: Whether the alert was checked against a current TNS snapshot.
            Always emitted as ``tns_checked`` so a consumer can distinguish a
            genuine non-match (checked, no ``tns`` block) from an unchecked/stale
            enrichment (not checked).
        tns_snapshot_epoch: The snapshot's timestamp used for the check, emitted
            as an ISO-8601 string under ``tns_snapshot_epoch`` (``None`` when not
            checked).

    Returns:
        A JSON-native dict with stable keys ``diaObjectId``, ``ra``, ``dec``,
        ``catalog_name``, ``catalog_source_id``, ``separation_arcsec``,
        ``catalog_payload``, ``catalogs_skipped`` (sorted list), ``partial``
        (true iff any catalog was skipped), ``tns_checked`` and
        ``tns_snapshot_epoch`` (the enrichment indicator), plus a nested ``tns``
        object when the alert associated with a TNS object.
    """
    skipped = sorted(catalogs_skipped) if catalogs_skipped else []
    payload = {
        'diaObjectId': int(dia_object_id),
        'ra': float(source_ra_deg),
        'dec': float(source_dec_deg),
        'catalog_name': catalog_name,
        'catalog_source_id': catalog_source_id,
        'separation_arcsec': float(separation_arcsec),
        'catalog_payload': catalog_payload,
        'catalogs_skipped': skipped,
        'partial': bool(skipped),
        'tns_checked': bool(tns_checked),
        'tns_snapshot_epoch': _epoch_to_iso(tns_snapshot_epoch),
    }
    if tns is not None:
        payload['tns'] = _tns_block(tns)
    return payload
