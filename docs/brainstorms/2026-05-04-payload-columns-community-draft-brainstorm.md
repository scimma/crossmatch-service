# Brainstorm: Catalog Payload Columns by PI Keyword (Community Review Draft)

**Date:** 2026-05-04

**Status:** Draft for alert broker community review (incorporates PI feedback
on [v1])

**Branch:** feature/catalog-specific-payload

**Predecessor:** [`docs/brainstorms/2026-04-27-payload-columns-by-keyword-brainstorm.md`][v1]

## 1. Summary

Concrete proposal for the **core** column set
that downstream subscribers receive when a Rubin DIA object matches a
row in one of our four crossmatch catalogs (Gaia DR3, DES Y6 Gold, DELVE DR3
Gold, SkyMapper DR4). This draft applies the PI's review of [v1]: a single
core tier, fewer keywords, photo-z but not RV. We are circulating it to
gather Rubin-community input on photometry defaults, multi-band rollup,
quality flags, and a small number of remaining product questions before
implementation planning begins.

---

## 2. Problem Frame

The crossmatch service ingests DIA-object alerts, performs spatial
crossmatches against several wide-area catalogs, and (in the near future)
will publish a per-match payload to downstream subscribers via Hopskotch.
Today the matched-row payload field is empty: only `source_id`, `ra`, `dec`
are loaded from the catalog side, and no columns are propagated to
subscribers.

The PI has supplied a list of keywords describing what kinds of catalog
information matter to downstream users (brightness, location, shape,
photo-z, classification, PSF). Each keyword maps onto specific columns
differently per catalog, and several PI keywords have multiple plausible
readings. [v1] enumerated a Core/Optional column proposal per keyword ×
catalog and was reviewed with the PI; this draft applies that review and is
now circulated to the wider alert broker community to settle the questions
the PI deferred.

---

## 3. Actors

- A1. **Rubin transient and multi-messenger followup teams** — alert
  brokers, ToO teams, multi-messenger groups consuming DIA-object alerts
  and the matched-catalog payload. Tend to care most about PSF photometry
  and a fast star/galaxy/QSO call.
- A2. **Galaxy / host-science groups** — researchers using crossmatch
  alerts to study DIA-object hosts, photo-z, morphology. Tend to care most
  about AUTO/BDF photometry, photo-z, shape parameters.
- A3. **Crossmatch service maintainers** — own the payload schema, the
  column-loading code, and the Hopskotch publishing path.
- A4. **Project PI** — has defined the keyword set, has reviewed [v1], and
  will sign off on the final core list once community input is incorporated.

---

## 4. Catalogs in scope

| Key             | Catalog          | HATS source                                               | Column-name convention      |
| --------------- | ---------------- | --------------------------------------------------------- | --------------------------- |
| `gaia_dr3`      | Gaia DR3         | `s3://stpubdata/gaia/gaia_dr3/public/hats`                | lowercase                   |
| `des_y6_gold`   | DES Y6 Gold      | `https://data.lsdb.io/hats/des/des_y6_gold`               | UPPERCASE                   |
| `delve_dr3_gold`| DELVE DR3 Gold   | `https://data.lsdb.io/hats/delve/delve_dr3_gold`          | UPPERCASE                   |
| `skymapper_dr4` | SkyMapper DR4    | `https://data.lsdb.io/hats/skymapper_dr4/catalog`         | lowercase, J2000 suffix     |

No new catalogs and no companion tables (Gaia BP/RP spectra, QSO/galaxy
candidates, DES Y6 metacal, etc.) are added by this proposal, but
additional catalogs will be incorporated in a later release of the
crossmatch service.

---

## 5. How column existence was verified

Every column name proposed below has been confirmed against the parquet
`_common_metadata` schema of the live HATS catalog as currently hosted on
`data.lsdb.io` (and the schema-identical Gaia DR3 mirror on AWS). This
verifies that each column **exists** in the catalog with a known dtype;
semantic interpretations (e.g., `EXT_MASH` encoding values, units of
`BDF_T`, the recommended-default status of `WAVG_MAG_PSF` vs alternatives)
come from catalog release notes and are part of what we are asking the
community to confirm. Per-catalog column listings, types, and counts are at:

- [`docs/references/gaia_dr3-columns.md`][gaia-cols] (153 columns)
- [`docs/references/des_y6_gold-columns.md`][des-cols] (337 columns)
- [`docs/references/delve_dr3_gold-columns.md`][delve-cols] (253 columns)
- [`docs/references/skymapper_dr4-columns.md`][skymapper-cols] (122 columns)

If any column below is unfamiliar, it is in the live catalog — see the
references for dtype.

---

## 6. PI keyword interpretation (post-review)

The PI reviewed [v1]'s keyword list and provided the following direction. The
new keyword set used in this draft reflects those edits.

| Keyword                  | Interpretation in this draft                                                                       | Status after PI review      |
| ------------------------ | -------------------------------------------------------------------------------------------------- | --------------------------- |
| brightness               | Magnitudes (per-band where applicable).                                                             | Confirmed                   |
| location                 | Sky position, position uncertainty (where available), proper motion, parallax (Gaia only).          | Confirmed                   |
| shape                    | Galaxy shape parameters: ellipticity, semi-axes, BDF size and de Vaucouleurs fraction.              | Confirmed                   |
| distributions / redshift | Photo-z **point estimate** (and width). Not stellar RV. Not SED / spectrum summary.                 | Refined — see notes below   |
| classification           | Star / galaxy / QSO label or probability. Subsumes "categorization".                                | Confirmed                   |
| psf-*                    | PSF-fit photometry and PSF-model diagnostic columns.                                                | Confirmed                   |
| categorization           | _Folded into classification._                                                                       | Folded per PI               |

### 6.1 Notes on "distributions" / "redshift"

The PI confirmed that photo-z is the intended meaning, and ruled out the
SED / spectrum-summary interpretation (Gaia BP/RP spectra, etc.). Stellar
radial velocity is also out: Gaia DR3's `radial_velocity` is **not** part
of the core payload.

For DES Y6 Gold and DELVE DR3 Gold this collapses to the DNF photo-z
columns (`DNF_Z`, `DNF_ZSIGMA`). For Gaia DR3 main and SkyMapper DR4, no
photo-z exists in the live HATS schemas, so this keyword has no
contribution from those catalogs in the core payload.

This draft assumes the photo-z payload is the **point estimate plus its
width** (summary-stats reading), not the full p(z) PDF. Including the full
PDF is a meaningful payload-size and schema decision and is surfaced as an
open question for the community below.

---

## 7. Key decisions (incorporating PI feedback)

1. **Single tier (core only) for now.** [v1] proposed two tiers (Core and
   Optional). The PI directed us to "start with the cores" and defer the
   optional sets. We will revisit an optional / extended tier after
   community input shapes the core.
2. **Keyword set is reduced.** Drop `moments`, `spiral`, `elliptical` from
   this iteration. Fold `categorization` into `classification`.
3. **No stellar RV in `redshift`.** Gaia DR3 `radial_velocity` is excluded.
   The redshift / distributions buckets are about photo-z only, and where a
   catalog has no photo-z, those buckets are empty rather than backfilled
   with stellar RV.
4. **No SED / spectrum-summary interpretation of `distributions`.** No
   Gaia BP/RP spectra or summary spectra. Companion tables stay out of
   scope.
5. **No spiral-vs-elliptical proxy in core.** [v1] noted that BDF parameters
   can act as an elliptical-vs-disk proxy; the PI elected to drop the
   keyword pair entirely rather than ship a proxy.

---

## 8. Per-catalog core column proposals

The lists below are the proposed core payload per keyword for each catalog.
Any column unavailable in a given catalog is marked as such — we do not
synthesize a substitute.

### 8.1 Gaia DR3 — `gaia_dr3`

Source table: `gaia_source` (DR3 main source). Lowercase columns. Gaia is
point-source by design; no galaxy shape, no photo-z in the main table.

- **brightness** — `phot_g_mean_mag`, `phot_bp_mean_mag`, `phot_rp_mean_mag`
- **location** — `ra`, `dec`, `parallax`, `pmra`, `pmdec`, `ref_epoch`
- **shape** — _none. Gaia is point-source by design._
- **distributions / redshift** — _none in the core payload. No photo-z in
  the main HATS catalog; stellar `radial_velocity` is excluded per PI
  direction._
- **classification** — `classprob_dsc_combmod_star`,
  `classprob_dsc_combmod_galaxy`, `classprob_dsc_combmod_quasar` (Discrete
  Source Classifier combined-mode probabilities)
- **psf-*** — `astrometric_excess_noise`, `ruwe` (Gaia does not publish a
  separate PSF magnitude; these are the standard PSF-fit-residual
  diagnostics)

### 8.2 DES Y6 Gold — `des_y6_gold`

UPPERCASE columns. Five bands (g, r, i, z, Y). DECam pipeline; BDF fit, no
metacal in this LSDB variant. DNF photo-z available.

- **brightness** — `MAG_AUTO_G`, `MAG_AUTO_R`, `MAG_AUTO_I`, `MAG_AUTO_Z`,
  `MAG_AUTO_Y`
- **location** — `RA`, `DEC`, `EBV_SFD98` (Galactic extinction map value,
  for downstream dereddening)
- **shape** — `BDF_T`, `BDF_G_1`, `BDF_G_2`, `BDF_FRACDEV`
- **distributions / redshift** — `DNF_Z`, `DNF_ZSIGMA`
- **classification** — `EXT_MASH` (recommended Y6 Gold star/galaxy
  separator: 0 = star, 4 = galaxy, intermediate values graded)
- **psf-*** — `WAVG_MAG_PSF_G`, `WAVG_MAG_PSF_R`, `WAVG_MAG_PSF_I`,
  `WAVG_MAG_PSF_Z`, `WAVG_MAG_PSF_Y`

*Photometry assignment is provisional pending community input on the
photometry default — see Outstanding Questions. `MAG_AUTO_*` are **not**
extinction-corrected; consumers can apply `EBV_SFD98` themselves, or the
community may prefer `BDF_MAG_*_CORRECTED` as the default.*

### 8.3 DELVE DR3 Gold — `delve_dr3_gold`

UPPERCASE columns. Four bands (g, r, i, z) — no Y band. Same DECam pipeline
as DES Y6 Gold; BDF, DNF photo-z, MASH classifier.

- **brightness** — `MAG_AUTO_G`, `MAG_AUTO_R`, `MAG_AUTO_I`, `MAG_AUTO_Z`
- **location** — `RA`, `DEC`, `EBV_SFD98` (Galactic extinction map value,
  for downstream dereddening)
- **shape** — `BDF_T`, `BDF_G_1`, `BDF_G_2`, `BDF_FRACDEV`
- **distributions / redshift** — `DNF_Z`, `DNF_ZSIGMA`
- **classification** — `EXT_MASH`
- **psf-*** — `WAVG_MAG_PSF_G`, `WAVG_MAG_PSF_R`, `WAVG_MAG_PSF_I`,
  `WAVG_MAG_PSF_Z`

*Photometry assignment is provisional pending community input on the
photometry default — see Outstanding Questions. `MAG_AUTO_*` are **not**
extinction-corrected; consumers can apply `EBV_SFD98` themselves, or the
community may prefer `BDF_MAG_*_CORRECTED` as the default.*

### 8.4 SkyMapper DR4 — `skymapper_dr4`

Lowercase columns, J2000 suffix on coordinates. Six bands (u, v, g, r, i,
z). The LSDB-hosted DR4 main catalog has **no shape/moment/image-ellipse
columns** and **no photo-z** — only `class_star` for morphological
information, and the PSF / Petrosian / fixed-aperture magnitude families
for photometry.

- **brightness** — `u_psf`, `v_psf`, `g_psf`, `r_psf`, `i_psf`, `z_psf`
- **location** — `raj2000`, `dej2000`, `e_raj2000`, `e_dej2000`
- **shape** — _not available in the SkyMapper DR4 main catalog._
- **distributions / redshift** — _not available in the SkyMapper DR4 main
  catalog._
- **classification** — `class_star` (continuous SExtractor classifier; ~1 =
  star, ~0 = galaxy)
- **psf-*** — `u_psf`, `v_psf`, `g_psf`, `r_psf`, `i_psf`, `z_psf`,
  `chi2_psf`, `flags_psf`

*The six PSF magnitude columns (`u_psf`–`z_psf`) are intentionally shared
between SkyMapper's `brightness` and `psf-*` buckets. Unlike DES/DELVE —
which have separate galaxy-friendly (`MAG_AUTO_*`, `BDF_MAG_*`) and stellar
(`WAVG_MAG_PSF_*`) families — SkyMapper's main catalog has no AUTO/BDF
families, so PSF photometry plays both roles. The `psf-*` bucket adds
`chi2_psf` and `flags_psf` as PSF-fit diagnostics. Implementations should
serialize these six columns once.*

---

## 9. Outstanding Questions

### 9.1 Resolve before planning (community input requested)

These are the questions left after the PI's [v1] review.

1. **Photometry default for DES Y6 Gold and DELVE DR3 Gold (including
   extinction correction).** The current core uses `MAG_AUTO_*`
   (uncorrected for Galactic extinction) for `brightness` and
   `WAVG_MAG_PSF_*` for `psf-*`. The choice between `MAG_AUTO_*`
   (uncorrected total flux), `BDF_MAG_*_CORRECTED` (dereddened,
   galaxy-friendly), and `WAVG_MAG_PSF_*` (stellar-friendly) is also a
   choice between extinction-corrected and uncorrected magnitudes. Should
   we ship corrected, uncorrected, both, or rely on `EBV_SFD98` (now in
   core) so consumers can deredden themselves?
2. **Multi-band rollup.** The current core enumerates every band per
   catalog (e.g., `MAG_AUTO_G/R/I/Z/Y` is five columns). For a typical
   alert payload, do consumers need every band, a reference subset (e.g.,
   `r` and `i` only), or all bands?
3. **Quality flags by default.** None of the cores above include the
   standard quality / footprint flags (`FLAGS_GOLD`, `FLAGS_FOREGROUND`,
   `FLAGS_FOOTPRINT`, `BDF_FLAGS`, `IMAFLAGS_ISO_*` for DES/DELVE;
   `flags`, `nimaflags`, `ngood` for SkyMapper; for Gaia `ruwe` and
   `astrometric_excess_noise` are already in the `psf-*` core). Should the
   others always be in the core regardless of keyword selection?
4. **SkyMapper pre-computed crossmatch IDs.** SkyMapper DR4 carries
   pre-computed crossmatch identifiers and distances to many neighbouring
   surveys — Gaia DR3, 2MASS, AllWISE, CatWISE, RefCat2, PS1, GALEX, VHS,
   LegacySurvey, DES DR2, NSC DR2, S-PLUS DR3 (e.g., `gaia_dr3_id1`,
   `twomass_key`, `allwise_cntr`, `ps1_dr1_id`, etc.). Not requested by
   any PI keyword, but a low-cost addition that lets consumers fetch
   matched rows in other surveys without re-running a positional
   crossmatch. Worth including?
5. **Which DNF point estimate and which DNF uncertainty for DES/DELVE
   photo-z?** DNF publishes `DNF_Z` (directional fit), `DNF_ZN`
   (nearest-neighbor), `DNF_ZSIGMA` (width), `DNF_ZERR_FIT` (fitting
   error), and `DNF_ZERR_PARAM` (parameter error). The current core ships
   `DNF_Z` + `DNF_ZSIGMA`, but DNF documentation often advises using both
   `DNF_Z` and `DNF_ZN` for outlier diagnosis, and the decomposed errors
   carry distinct physical meaning. Should the core add `DNF_ZN` and/or
   the decomposed errors?
6. **Photo-z PDF vs summary stats.** The current core ships `DNF_Z` +
   `DNF_ZSIGMA` (a point estimate and a width). Does any community group
   need the full p(z) PDF? If yes, this changes payload size meaningfully
   and probably argues for a "PDF-on-request" extension rather than
   including the PDF in every alert.

---

## 10. Scope Boundaries

- **Optional / extended payload tier.** Deferred per PI direction; will be
  revisited after community input on the core.
- **New catalogs.** No additions to the catalog set in this proposal.

---

[v1]: 2026-04-27-payload-columns-by-keyword-brainstorm.md
[gaia-cols]: ../references/gaia_dr3-columns.md
[des-cols]: ../references/des_y6_gold-columns.md
[delve-cols]: ../references/delve_dr3_gold-columns.md
[skymapper-cols]: ../references/skymapper_dr4-columns.md
