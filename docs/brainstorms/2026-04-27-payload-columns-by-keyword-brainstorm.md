# Brainstorm: Catalog Payload Columns by PI Keyword

**Date:** 2026-04-27
**Status:** Draft for PI review (column names verified against live HATS schemas)
**Branch:** feature/catalog-specific-payload

## Purpose

The PI provided a list of keywords describing what kinds of catalog information a downstream subscriber should receive when a DIA object matches a row in one of our crossmatch catalogs. This document proposes, for each catalog, a **two-tier set of columns per keyword**:

- **Core** — the recommended default included in every payload.
- **Optional** — additional columns the PI may want for an extended payload, or behind a per-subscriber opt-in.

The output target is the `CatalogMatch.catalog_payload` JSONB field (`crossmatch/core/models.py`) which is currently `null`. Selected columns will also be loaded by `crossmatch/matching/catalog.py` (today only `source_id`, `ra`, `dec` are loaded).

This is a starting list to anchor the conversation with the PI. Final selections, units, and naming should be confirmed by the PI before implementation planning.

## How column names were verified

Every column name below was confirmed against the parquet `_common_metadata` schema of each catalog as currently hosted on `data.lsdb.io` (and the schema-identical Gaia DR3 mirror). The dump script lives at `scripts/dump_catalog_columns.py` and the full per-catalog column listings are at:

- `docs/references/gaia_dr3-columns.md` (153 columns)
- `docs/references/des_y6_gold-columns.md` (337 columns)
- `docs/references/delve_dr3_gold-columns.md` (253 columns)
- `docs/references/skymapper_dr4-columns.md` (122 columns)

If any column below is unfamiliar, it is in the live catalog — see the references for dtype.

## Catalogs in scope

Configured in `crossmatch/project/settings.py:22-58`:

| Key             | Catalog          | HATS source                                               | Column-name convention      |
| --------------- | ---------------- | --------------------------------------------------------- | --------------------------- |
| `gaia_dr3`      | Gaia DR3         | `s3://stpubdata/gaia/gaia_dr3/public/hats`                | lowercase                   |
| `des_y6_gold`   | DES Y6 Gold      | `https://data.lsdb.io/hats/des/des_y6_gold`               | UPPERCASE                   |
| `delve_dr3_gold`| DELVE DR3 Gold   | `https://data.lsdb.io/hats/delve/delve_dr3_gold`          | UPPERCASE                   |
| `skymapper_dr4` | SkyMapper DR4    | `https://data.lsdb.io/hats/skymapper_dr4/catalog`         | lowercase, J2000 suffix     |

## PI keyword interpretation

Some PI keywords are unambiguous (brightness, location, redshift). Others have multiple plausible readings — those are flagged here. **Please confirm or refine these interpretations.**

| Keyword          | Interpretation used in this draft                                                                       | Confirmation needed? |
| ---------------- | ------------------------------------------------------------------------------------------------------- | -------------------- |
| brightness       | Magnitudes and fluxes (per-band where applicable).                                                      | No                   |
| location         | Sky position, position uncertainty, proper motion, parallax.                                            | No                   |
| shape            | Galaxy shape parameters: ellipticity, semi-axes, position angle, BDF size, Kron/Petrosian radii.        | No                   |
| moments          | Second-moment / shear estimates. In the in-scope catalogs this collapses to BDF or shape proxies.       | Yes — keep distinct, or fold into shape? |
| distributions    | Photo-z PDFs, SEDs, or summary statistics of distributions (mean, sigma).                               | **Yes** — see note below. |
| redshift         | Photometric or spectroscopic redshift, point estimate plus uncertainty.                                 | No                   |
| classification   | Coarse type label or probability (star / galaxy / QSO).                                                 | Yes — same as categorization? |
| spiral           | Late-type / disk-galaxy morphology label.                                                               | Yes — no in-scope catalog publishes this directly. |
| elliptical       | Early-type / elliptical morphology label.                                                               | Yes — no in-scope catalog publishes this directly. |
| categorization   | Treated as overlapping with **classification**.                                                         | Yes — distinct from classification? |
| psf-*            | PSF-fit photometry and PSF-model diagnostic columns.                                                    | No                   |

### "distributions" — please clarify

Three plausible readings:

1. **Photo-z PDF**: full p(z) distribution. Not present in the main HATS tables for any of the four catalogs as configured today.
2. **Summary statistics of distributions**: mean and sigma of photo-z, RV distribution, etc. — covered by **redshift** + uncertainty columns.
3. **SED / spectrum summary**: BP/RP spectra summary in Gaia (`has_xp_continuous`, `has_xp_sampled` flags exist; the actual spectra live in companion tables).

The draft below assumes (2) — summary statistics. If the PI means (1) full PDFs, this changes payload size meaningfully and we should plan a separate "PDF-on-request" path. If (3), the brainstorm needs to scope adding companion tables (Gaia XP, etc.).

### "spiral" / "elliptical"

None of the in-scope catalogs publish a Hubble-type or T-type morphological label. The closest available signals are:

- **DES Y6 Gold and DELVE DR3 Gold**: BDF (bulge+disk fit) parameters — `BDF_FRACDEV` (de Vaucouleurs fraction; high → elliptical), `BDF_G_1`/`BDF_G_2` (ellipticity components), `BDF_T` (size).
- **SkyMapper DR4**: only `class_star` (continuous star/galaxy) and PSF χ² — no shape / morphology columns.
- **Gaia DR3**: classifier probabilities for star / galaxy / QSO; no spiral-vs-elliptical distinction.

The draft surfaces these as proxies and notes "no native spiral-vs-elliptical label" where relevant. A true spiral/elliptical flag would require crossmatching to Galaxy Zoo, RC3, or a morphology-specific catalog — out of scope here.

---

## Gaia DR3 — `gaia_dr3` (153 columns)

Source table: `gaia_source` (the canonical DR3 main source table). Lowercase columns.

### brightness
- **Core**: `phot_g_mean_mag`, `phot_bp_mean_mag`, `phot_rp_mean_mag`
- **Optional**: `phot_g_mean_flux`, `phot_g_mean_flux_error`, `phot_bp_mean_flux`, `phot_bp_mean_flux_error`, `phot_rp_mean_flux`, `phot_rp_mean_flux_error`, `bp_rp`, `bp_g`, `g_rp`, `phot_g_n_obs`, `phot_bp_n_obs`, `phot_rp_n_obs`, `phot_variable_flag`, `phot_bp_rp_excess_factor`, `grvs_mag`, `grvs_mag_error`
- **Notes**: Vega-like Gaia photometric system. `phot_*_n_obs` indicates how many transits contributed. `grvs_mag` is the integrated RVS-band magnitude (only present for the ~30M sources with RVS).

### location
- **Core**: `ra`, `dec`, `parallax`, `pmra`, `pmdec`, `ref_epoch`
- **Optional**: `ra_error`, `dec_error`, `parallax_error`, `parallax_over_error`, `pmra_error`, `pmdec_error`, `pm`, `l`, `b`, `ecl_lon`, `ecl_lat`, `ruwe`, `astrometric_excess_noise`, `astrometric_excess_noise_sig`, `astrometric_sigma5d_max`, `radial_velocity`, `radial_velocity_error`, `distance_gspphot`, `distance_gspphot_lower`, `distance_gspphot_upper`
- **Notes**: `ref_epoch` is essential for downstream consumers that propagate the Gaia position to the alert epoch. `ruwe` doubles as an astrometric quality flag (well-behaved single stars: `ruwe ≲ 1.4`). `distance_gspphot` is the Bayesian distance estimate from BP/RP/parallax — the most common "stellar distance" for Gaia DR3.

### shape
- **Core**: _none — Gaia is point-source by design._
- **Optional**: `ipd_gof_harmonic_amplitude`, `ipd_gof_harmonic_phase`, `ipd_frac_multi_peak`, `ipd_frac_odd_win`
- **Notes**: Gaia does not report galaxy shape parameters. The IPD (image parameter determination) columns can flag non-pointlike or binary sources.

### moments
- _Not applicable for Gaia DR3 main source table._ See `shape` notes above.

### distributions
- **Core**: _none in the main source table._
- **Optional**: `phot_bp_rp_excess_factor` (BP+RP vs G flux ratio — broad SED proxy), `has_xp_continuous`, `has_xp_sampled`, `has_rvs` (boolean flags indicating that BP/RP or RVS spectra exist for the source in companion tables)
- **Notes**: The actual BP/RP sampled mean spectra live in the `xp_sampled_mean_spectrum` companion table, not in `gaia_source` and therefore not in our HATS catalog as configured today. If the PI wants spectra, a separate companion HATS source is needed (out of scope).

### redshift
- **Core**: `radial_velocity`, `radial_velocity_error`
- **Optional**: `rv_template_teff`, `rv_template_logg`, `rv_template_fe_h`, `rv_nb_transits`, `rv_renormalised_gof`, `vbroad`, `vbroad_error`, `grvs_mag`
- **Notes**: Main Gaia source table provides stellar radial velocity, **not** photometric redshift for galaxies. QSO/galaxy redshift estimates exist in `qso_candidates` / `galaxy_candidates` companion tables (not in our main HATS catalog). If the PI wants those, we will need to add a second Gaia HATS source.

### classification
- **Core**: `classprob_dsc_combmod_star`, `classprob_dsc_combmod_galaxy`, `classprob_dsc_combmod_quasar`
- **Optional**: `phot_variable_flag`, `non_single_star`, `in_qso_candidates`, `in_galaxy_candidates`, `in_andromeda_survey`, `teff_gspphot`, `logg_gspphot`, `mh_gspphot`, `ag_gspphot`, `azero_gspphot`, `ebpminrp_gspphot`, `libname_gspphot`, `has_mcmc_gspphot`, `has_mcmc_msc`
- **Notes**: DSC = Discrete Source Classifier. The three combined-mode probabilities sum to ~1 and are the cleanest classification signal in Gaia DR3. The GSP-Phot stellar parameters (`teff_gspphot`, `logg_gspphot`, `mh_gspphot`) are only populated for sources with reliable spectrophotometric solutions; expect nulls.

### spiral
- _Not available in Gaia DR3._

### elliptical
- _Not available in Gaia DR3._

### categorization
- See **classification** above. No Gaia-specific separate categorization columns.

### psf-*
- **Core**: `astrometric_excess_noise`, `ruwe`
- **Optional**: `astrometric_chi2_al`, `astrometric_n_obs_al`, `astrometric_n_good_obs_al`, `astrometric_gof_al`, `ipd_gof_harmonic_amplitude`, `ipd_frac_multi_peak`
- **Notes**: Gaia does not publish a separate "PSF magnitude" the way SDSS or DES do. `ruwe` and `astrometric_excess_noise` are the standard PSF-fit-residual diagnostics.

---

## DES Y6 Gold — `des_y6_gold` (337 columns)

UPPERCASE columns. The LSDB-hosted Y6 Gold catalog uses `PSF_MAG_APER_8_*` (PSF magnitude in an 8-pixel-equivalent aperture) and `WAVG_MAG_PSF_*` (weighted-average PSF magnitude across single-epoch detections) — there is **no `MAG_PSF_*` column**. The shape catalog (metacalibration / second moments) is **not** included in this LSDB variant; shape information comes from BDF (bulge+disk fit).

### brightness
- **Core**: `MAG_AUTO_G`, `MAG_AUTO_R`, `MAG_AUTO_I`, `MAG_AUTO_Z`, `MAG_AUTO_Y`
- **Optional**: `MAGERR_AUTO_G/R/I/Z/Y`, `BDF_MAG_G/R/I/Z/Y`, `BDF_MAG_ERR_G/R/I/Z/Y`, `BDF_MAG_*_CORRECTED` (extinction-corrected), `MAG_DETMODEL_G/R/I/Z/Y`, `MAGERR_DETMODEL_*`, `MAG_APER_4_G/R/I/Z/Y`, `MAG_APER_8_G/R/I/Z/Y`, `GAP_MAG_G/R/I/Z`, `GAP_MAG_ERR_G/R/I/Z` (Gaussian-aperture mag — galaxy-friendly), `EBV_SFD98` (extinction map value), `A_FIDUCIAL_G/R/I/Z/Y` (per-band extinction at fiducial position)
- **Notes**: Y6 Gold convention is to use `BDF_MAG_*_CORRECTED` for galaxy colors and `MAG_AUTO_*` for total flux. Confirm with PI which (or both) belong in the payload.

### location
- **Core**: `RA`, `DEC`
- **Optional**: `ALPHAWIN_J2000`, `DELTAWIN_J2000`, `XWIN_IMAGE`, `YWIN_IMAGE`, `GLON`, `GLAT`, `HPIX_4096`, `HPIX_16384`, `EBV_SFD98`
- **Notes**: Y6 Gold positions are on the Gaia reference frame at a survey-mean epoch; no proper motions in Y6 Gold itself. There is no explicit per-coordinate uncertainty column in the main Y6 Gold catalog.

### shape
- **Core**: `BDF_T`, `BDF_G_1`, `BDF_G_2`, `BDF_FRACDEV`
- **Optional**: `BDF_T_ERR`, `BDF_T_RATIO`, `BDF_FRACDEV_ERR`, `BDF_G_COV_1_1`, `BDF_G_COV_1_2`, `BDF_G_COV_2_1`, `BDF_G_COV_2_2`, `A_IMAGE`, `B_IMAGE`, `THETA_J2000`, `ERRA_IMAGE`, `ERRB_IMAGE`, `ERRTHETA_IMAGE`, `KRON_RADIUS`, `FLUX_RADIUS_G/R/I/Z/Y`, `CONC` (concentration index)
- **Notes**: BDF is the recommended shape source in Y6 Gold for non-shear analyses. `BDF_G_1`/`BDF_G_2` are ellipticity components; `BDF_T` is the size; `BDF_FRACDEV` is the de Vaucouleurs fraction (high → bulge-dominated → likely elliptical).

### moments
- **Core**: `BDF_G_1`, `BDF_G_2`, `BDF_T`
- **Optional**: full 2×2 BDF covariance via `BDF_G_COV_*`, plus `BDF_FLUX_COV_*` for flux-flux covariances; `PSF_G_1`, `PSF_G_2`, `PSF_T` (PSF model moments at the source position)
- **Notes**: The metacalibration shape catalog with raw `Ixx`/`Iyy`/`Ixy` and metacal responses is **not** in this LSDB variant. If true second moments are required, we would need to add the Y6 metacal catalog as a separate HATS source.

### distributions
- **Core**: `DNF_Z`, `DNF_ZSIGMA`
- **Optional**: `DNF_ZERR_FIT`, `DNF_ZERR_PARAM`, `DNF_NNEIGHBORS`, `DNF_ID1`, `DNF_D1`, `DNF_DE1`, `DNF_ZN`
- **Notes**: This LSDB Y6 Gold variant carries DNF photo-z only — there are **no BPZ photo-z columns** in the schema. `DNF_Z` is the point estimate; `DNF_ZSIGMA` is the width; the `*_FIT`/`*_PARAM` errors decompose fitting vs. parameter uncertainty.

### redshift
- See **distributions** above. The same `DNF_Z` / `DNF_ZSIGMA` columns serve as the redshift point estimate.

### classification
- **Core**: `EXT_MASH` — the recommended Y6 Gold star/galaxy separator (0 = star, 4 = galaxy, intermediate values graded).
- **Optional**: `EXT_COADD`, `EXT_WAVG`, `EXT_FITVD`, `EXT_XGB`, `XGB_PRED`, `WAVG_SPREAD_MODEL_I`, `WAVG_SPREADERR_MODEL_I`, `SPREAD_MODEL_I`, `SPREADERR_MODEL_I`
- **Notes**: Y6 Gold's MASH classifier consolidates several SExtractor outputs and is the standard recommendation in the survey papers. `EXT_XGB` and `XGB_PRED` are the gradient-boosted classifier outputs; useful as a second opinion.

### spiral
- _No native label._ Closest proxy: `EXT_MASH ≥ 3` (galaxy) plus low `BDF_FRACDEV` (disk-dominated) plus high `|BDF_G_1| + |BDF_G_2|` (elongated).

### elliptical
- _No native label._ Closest proxy: `EXT_MASH ≥ 3` (galaxy) plus high `BDF_FRACDEV` (bulge-dominated).

### categorization
- See **classification** above.

### psf-*
- **Core**: `WAVG_MAG_PSF_G`, `WAVG_MAG_PSF_R`, `WAVG_MAG_PSF_I`, `WAVG_MAG_PSF_Z`, `WAVG_MAG_PSF_Y`
- **Optional**: `WAVG_MAGERR_PSF_G/R/I/Z/Y`, `PSF_MAG_APER_8_G/R/I/Z/Y`, `PSF_MAG_ERR_APER_8_G/R/I/Z/Y`, `PSF_MAG_APER_8_*_CORRECTED`, `PSF_FLUX_APER_8_G/R/I/Z/Y`, `PSF_FLUX_ERR_APER_8_*`, `PSF_FLUX_S2N_APER_8_*`, `PSF_FLUX_RATIO_APER_8_*`, `PSF_T`, `PSF_G_1`, `PSF_G_2`, `WAVG_SPREAD_MODEL_G/R/I/Z/Y`, `WAVG_SPREADERR_MODEL_*`, `SPREAD_MODEL_G/R/I/Z/Y`, `SPREADERR_MODEL_*`
- **Notes**: PSF magnitudes are critical for stellar work and arguably belong in the **core** payload alongside `MAG_AUTO_*`. The WAVG variants are the weighted average across single-epoch detections; the APER_8 variants are coadd PSF magnitudes. The PI may prefer one over the other.

### Y6 Gold quality flags worth surfacing regardless of keyword
- `FLAGS_GOLD`, `FLAGS_FOREGROUND`, `FLAGS_FOOTPRINT`, `FLAGSTR`, `MASK_FLAGS`, `IMAFLAGS_ISO_G/R/I/Z/Y`, `BDF_FLAGS`, `BDF_DEBLEND_FLAGS`, `FITVD_FLAGS` — strongly recommended for any payload so downstream consumers can apply standard masks.

---

## DELVE DR3 Gold — `delve_dr3_gold` (253 columns)

UPPERCASE columns. DELVE survey, southern sky with DECam, value-added DR3 Gold. The schema is essentially DES Y6 Gold minus the Y band, minus the shear catalog — the same DECam pipeline, the same `BDF`/`PSF_MAG_APER_8`/`MAG_AUTO`/`DNF` conventions. **DELVE DR3 Gold does include DNF photo-z.**

### brightness
- **Core**: `MAG_AUTO_G`, `MAG_AUTO_R`, `MAG_AUTO_I`, `MAG_AUTO_Z`
- **Optional**: `MAGERR_AUTO_G/R/I/Z`, `BDF_MAG_G/R/I/Z`, `BDF_MAG_ERR_G/R/I/Z`, `BDF_MAG_*_CORRECTED`, `MAG_DETMODEL_G/R/I/Z`, `MAGERR_DETMODEL_*`, `GAP_MAG_G/R/I/Z`, `GAP_MAG_ERR_*`, `EBV_SFD98`, `A_FIDUCIAL_G/R/I/Z`
- **Notes**: DELVE DR3 Gold has only griz (no Y).

### location
- **Core**: `RA`, `DEC`
- **Optional**: `ALPHAWIN_J2000`, `DELTAWIN_J2000`, `XWIN_IMAGE`, `YWIN_IMAGE`, `GLON`, `GLAT`, `HPIX_4096`, `HPIX_16384`
- **Notes**: Like DES Y6 Gold, no proper motions and no per-coordinate uncertainty in the main catalog.

### shape
- **Core**: `BDF_T`, `BDF_G_1`, `BDF_G_2`, `BDF_FRACDEV`
- **Optional**: `BDF_T_ERR`, `BDF_T_RATIO`, `BDF_FRACDEV_ERR`, `BDF_G_COV_1_1/1_2/2_1/2_2`, `A_IMAGE`, `B_IMAGE`, `THETA_J2000`, `ERRA_IMAGE`, `ERRB_IMAGE`, `ERRTHETA_IMAGE`, `KRON_RADIUS`, `FLUX_RADIUS_G/R/I/Z`
- **Notes**: Same BDF-based shape model as DES Y6 Gold. No metacal in DELVE DR3 Gold.

### moments
- **Core**: `BDF_G_1`, `BDF_G_2`, `BDF_T`
- **Optional**: BDF covariance matrix elements `BDF_G_COV_*`, BDF flux covariance `BDF_FLUX_COV_*_*`, `PSF_G_1`, `PSF_G_2`, `PSF_T`
- **Notes**: No raw second-moment columns; BDF parameters are the closest equivalent.

### distributions
- **Core**: `DNF_Z`, `DNF_ZSIGMA`
- **Optional**: `DNF_ZERR_FIT`, `DNF_ZERR_PARAM`, `DNF_NNEIGHBORS`, `DNF_ID1`, `DNF_D1`, `DNF_DE1`, `DNF_ZN`
- **Notes**: DELVE DR3 Gold ships DNF photo-z; no BPZ.

### redshift
- See **distributions**.

### classification
- **Core**: `EXT_MASH`
- **Optional**: `EXT_COADD`, `EXT_WAVG`, `EXT_FITVD`, `EXT_XGB`, `XGB_PRED`, `SPREAD_MODEL_G/R/I/Z`, `SPREADERR_MODEL_*`, `WAVG_SPREAD_MODEL_*`, `WAVG_SPREADERR_MODEL_*`
- **Notes**: Same MASH classifier as Y6 Gold.

### spiral
- _No native label._ Same proxies as DES Y6 Gold.

### elliptical
- _No native label._ Same proxies as DES Y6 Gold.

### categorization
- See **classification**.

### psf-*
- **Core**: `WAVG_MAG_PSF_G`, `WAVG_MAG_PSF_R`, `WAVG_MAG_PSF_I`, `WAVG_MAG_PSF_Z`
- **Optional**: `WAVG_MAGERR_PSF_G/R/I/Z`, `PSF_MAG_APER_8_G/R/I/Z`, `PSF_MAG_ERR_APER_8_G/R/I/Z`, `PSF_MAG_APER_8_*_CORRECTED`, `PSF_FLUX_APER_8_*`, `PSF_FLUX_ERR_APER_8_*`, `PSF_FLUX_S2N_APER_8_*`, `PSF_FLUX_RATIO_APER_8_*`, `PSF_T`, `PSF_G_1`, `PSF_G_2`
- **Notes**: As with DES, PSF mags are core for stellar science.

### DELVE DR3 Gold quality flags worth surfacing regardless of keyword
- `FLAGS_GOLD`, `FLAGS_FOREGROUND`, `FLAGS_FOOTPRINT`, `FLAGSTR`, `IMAFLAGS_ISO_G/R/I/Z`, `BDF_FLAGS`, `BDF_DEBLEND_FLAGS`, `FITVD_FLAGS`, `S_EXTRACTOR_FLAGS_G/R/I/Z`.

---

## SkyMapper DR4 — `skymapper_dr4` (122 columns)

Lowercase columns, J2000 suffix on coordinates. SkyMapper Southern Survey DR4 (Onken et al.). Six bands: u, v, g, r, i, z. **The LSDB-hosted DR4 main catalog has no shape / moment / image-ellipse columns** — no `a`, `b`, `pa`, `Ixx`, etc. The only morphological information is `class_star`. The catalog also carries pre-computed crossmatch IDs and distances to many neighbouring surveys (Gaia DR3, 2MASS, AllWISE, CatWISE, RefCat2, PS1, GALEX, VHS, LegacySurvey, DES DR2, NSC DR2, S-PLUS DR3) — potentially useful for downstream consumers.

### brightness
- **Core**: `u_psf`, `v_psf`, `g_psf`, `r_psf`, `i_psf`, `z_psf` (PSF magnitudes — the primary photometry for point sources)
- **Optional**: `e_u_psf`, `e_v_psf`, `e_g_psf`, `e_r_psf`, `e_i_psf`, `e_z_psf`, `u_petro`, `v_petro`, `g_petro`, `r_petro`, `i_petro`, `z_petro`, `e_*_petro`, `u_apc05`, `v_apc05`, `g_apc05`, `r_apc05`, `i_apc05`, `z_apc05`, `e_*_apc05`, `u_mmvar`, `v_mmvar`, `g_mmvar`, `r_mmvar`, `i_mmvar`, `z_mmvar`, `radius_petro`, `mean_fwhm`, `ebmv_sfd`, `ebmv_gnilc`
- **Notes**: SkyMapper has six bands (uvgriz). PSF mags are the natural core for stellar science; Petrosian mags are better for galaxies — consider including both core sets if the PI wants both regimes covered. `apc05` is a fixed-aperture (5″) magnitude; `mmvar` is a millimag-scale variability indicator per band.

### location
- **Core**: `raj2000`, `dej2000`, `e_raj2000`, `e_dej2000`
- **Optional**: `glon`, `glat`, `mean_epoch`, `rms_epoch`, `smss_j` (the SkyMapper unique designation), `ebmv_sfd`, `ebmv_gnilc`, `ebmv_g_err`
- **Notes**: SkyMapper does not publish proper motions or parallax. `mean_epoch` is a per-source mean observation epoch and is useful for any downstream proper-motion correction.

### shape
- _Not available in SkyMapper DR4 main catalog._ The LSDB schema has no `a`, `b`, `pa`, image moments, or BDF parameters. Only `class_star` and the per-band `*_mmvar` variability indicator are present.

### moments
- _Not available in SkyMapper DR4 main catalog._ See **shape**.

### distributions
- _Not applicable — SkyMapper DR4 main catalog does not publish photo-z, SED fits, or spectral summaries._

### redshift
- _Not available in SkyMapper DR4 main catalog._

### classification
- **Core**: `class_star` (continuous SExtractor classifier; ~1 = star, ~0 = galaxy)
- **Optional**: `chi2_psf`, `flags_psf`, `flags`, `nimaflags`, `ngood`, `u_ngood`, `v_ngood`, `g_ngood`, `r_ngood`, `i_ngood`, `z_ngood`
- **Notes**: `class_star` is the only classifier in the main SkyMapper catalog. `chi2_psf` (PSF-fit χ²) is a useful auxiliary discriminator.

### spiral
- _No native label._ No shape parameters, so no proxy is available beyond `class_star`.

### elliptical
- _No native label._ Same caveat as spiral.

### categorization
- See **classification**.

### psf-*
- **Core**: `u_psf`, `v_psf`, `g_psf`, `r_psf`, `i_psf`, `z_psf`, `chi2_psf`, `flags_psf`
- **Optional**: `e_u_psf`, `e_v_psf`, `e_g_psf`, `e_r_psf`, `e_i_psf`, `e_z_psf`, `mean_fwhm`
- **Notes**: PSF mags are the primary photometry in SkyMapper for point sources; including them is essentially mandatory.

### Bonus: pre-computed crossmatch IDs (for joined-query downstream consumers)
- `gaia_dr3_id1`, `gaia_dr3_dist1`, `gaia_dr3_id2`, `gaia_dr3_dist2`, `cnt_gaia_dr3_15`
- `twomass_key`, `twomass_dist`
- `allwise_cntr`, `allwise_dist`
- `catwise_id`, `catwise_dist`
- `refcat2_id`, `refcat2_dist`
- `ps1_dr1_id`, `ps1_dr1_dist`
- `galex_guv_id`, `galex_guv_dist`
- `vhs_dr6_id`, `vhs_dr6_dist`
- `ls_dr9_id`, `ls_dr9_dist`
- `des_dr2_id`, `des_dr2_dist`
- `nsc_dr2_id`, `nsc_dr2_dist`
- `splus_dr3_id`, `splus_dr3_dist`
- **Notes**: Not requested by any of the PI's keywords, but easy wins to include because they let consumers fetch matched rows in other surveys without re-running a positional crossmatch. Worth flagging for the PI.

---

## Cross-cutting open questions for the PI

1. **Curation level confirmation** — This draft uses two tiers (Core / Optional). Comfortable with that, or prefer a single flat list per keyword?
2. **"distributions" interpretation** — Summary stats (mean, sigma) as assumed, or full PDFs / SEDs? See section above.
3. **"spiral" vs "elliptical" vs "classification" vs "categorization"** — Are these meant to be distinct buckets, or is the PI fine collapsing them given that none of the in-scope catalogs publish native Hubble types?
4. **Multi-band rollup** — In the doc above, multi-band photometry is enumerated explicitly (e.g., `MAG_AUTO_G/R/I/Z/Y`). Should the message payload include all bands, or only one or two reference bands?
5. **AUTO vs. BDF vs. PSF vs. PETRO photometry default** — DES/DELVE offer AUTO, BDF, DETMODEL, APER, GAP, and PSF magnitudes; SkyMapper offers PSF, PETRO, and APC05. Which is the default "magnitude" for the payload, and which are extras?
6. **Quality flags by default** — Should `FLAGS_GOLD`, `FLAGS_FOOTPRINT`, `FLAGS_FOREGROUND`, `BDF_FLAGS`, `IMAFLAGS_ISO_*` (DES/DELVE), `flags`, `nimaflags`, `ngood` (SkyMapper), and `ruwe`, `astrometric_excess_noise` (Gaia) be auto-included regardless of keyword selection? Strongly recommended.
7. **SkyMapper crossmatch IDs** — Worth surfacing the pre-computed IDs to Gaia DR3, 2MASS, AllWISE, etc. in the SkyMapper payload? Not in the keyword list but high-value and free.
8. **Missing data convention** — When a column is null in the catalog, should the payload omit the key or include it with a null value? (Affects schema stability for downstream consumers.)
9. **Total payload size budget** — Any per-message size target? Some optional sets above are large; a budget will let me trim.

## Out of scope for this brainstorm

- **Implementation design** — how columns flow from `crossmatch/matching/catalog.py` into `CatalogMatch.catalog_payload` and onwards through `crossmatch/notifier/impl_hopskotch.py`. That belongs in a follow-on plan once columns are agreed.
- **Per-subscriber payload customization** — useful future capability, but separate decision.
- **Adding new catalogs or companion tables** — Gaia QSO/galaxy candidate companion tables, Gaia BP/RP spectra, DES Y6 metacal shape catalog, or Galaxy Zoo for true morphology — all separate scope.
