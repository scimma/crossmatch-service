from time import sleep
from random import randint
from datetime import datetime
from tasks.crossmatch import crossmatch
from core.models import Alert
from core.log import get_logger
logger = get_logger(__name__)


def consume_alerts():
    logger.info('Listening to alert broker...')
    while True:
        try:
            alert = mock_alert_generator()
            alert_obj = Alert.objects.create(
                ra_deg=alert['lsst_diaObject_ra'],
                dec_deg=alert['lsst_diaObject_dec'],
                lsst_diaObject_diaObjectId=alert['lsst_diaObject_diaObjectId'],
                lsst_diaSource_diaSourceId=alert['lsst_diaSource_diaSourceId'],
                event_time=datetime.fromtimestamp(alert['ant_time_received']),
                payload=alert,
                status=Alert.Status.INGESTED,
            )
            logger.info(f'New alert ingested: {alert_obj}')
            logger.debug(f'Launching crossmatching task for alert {alert_obj}...')
            crossmatch.delay(alert_id=alert_obj.uuid)
        except Exception as err:
            logger.error(f'Error ingesting alert: {err}')
        # Pause for a random duration between 5 and 15 seconds
        sleep(randint(5, 15))


def mock_alert_generator():
    example_alert = {
        'ant_dec': 6.714927120071068,
        'ant_mag': 22.92294986302458,
        'ant_magerr': 0.05429195478473967,
        'ant_maglim': 22.92294986302458,
        'ant_mjd': 61096.37656126988,
        'ant_passband': 'g',
        'ant_ra': 186.6695224992812,
        'ant_survey': 4,
        'ant_time_received': 1772010265,
        'lsst_diaObject_dec': 6.714927120071068,
        'lsst_diaObject_decErr': 1.2797075214621145e-05,
        'lsst_diaObject_diaObjectId': 170055002004914266,
        'lsst_diaObject_g_psfFluxErrMean': 122.97907257080078,
        'lsst_diaObject_g_psfFluxMax': 2459.348388671875,
        'lsst_diaObject_g_psfFluxMean': 2459.348388671875,
        'lsst_diaObject_g_psfFluxMeanErr': 122.97908020019531,
        'lsst_diaObject_g_psfFluxMin': 2459.348388671875,
        'lsst_diaObject_g_psfFluxNdata': 1,
        'lsst_diaObject_g_scienceFluxMean': 2442.6640625,
        'lsst_diaObject_g_scienceFluxMeanErr': 120.82659149169922,
        'lsst_diaObject_i_psfFluxNdata': 0,
        'lsst_diaObject_nDiaSources': 1,
        'lsst_diaObject_r_psfFluxNdata': 0,
        'lsst_diaObject_ra': 186.66952249928116,
        'lsst_diaObject_raErr': 8.39069252833724e-06,
        'lsst_diaObject_ra_dec_Cov': -2.0313417614659102e-11,
        'lsst_diaObject_u_psfFluxNdata': 0,
        'lsst_diaObject_validityStartMjdTai': 61096.37837065163,
        'lsst_diaObject_y_psfFluxNdata': 0,
        'lsst_diaObject_z_psfFluxNdata': 0,
        'lsst_diaSource_apFlux': 2232.072998046875,
        'lsst_diaSource_apFluxErr': 284.48046875,
        'lsst_diaSource_apFlux_flag': False,
        'lsst_diaSource_apFlux_flag_apertureTruncated': False,
        'lsst_diaSource_band': 'g',
        'lsst_diaSource_bboxSize': 25,
        'lsst_diaSource_centroid_flag': False,
        'lsst_diaSource_dec': 6.714927120071068,
        'lsst_diaSource_decErr': 1.2797075214621145e-05,
        'lsst_diaSource_detector': 89,
        'lsst_diaSource_diaObjectId': 170055002004914266,
        'lsst_diaSource_diaSourceId': 170055002004914266,
        'lsst_diaSource_dipoleFitAttempted': False,
        'lsst_diaSource_dipoleNdata': 0,
        'lsst_diaSource_extendedness': 0.016484128311276436,
        'lsst_diaSource_forced_PsfFlux_flag': False,
        'lsst_diaSource_forced_PsfFlux_flag_edge': False,
        'lsst_diaSource_forced_PsfFlux_flag_noGoodPixels': False,
        'lsst_diaSource_glint_trail': False,
        'lsst_diaSource_isDipole': False,
        'lsst_diaSource_isNegative': False,
        'lsst_diaSource_ixx': 0.292331337928772,
        'lsst_diaSource_ixxPSF': 0.25552958250045776,
        'lsst_diaSource_ixy': -0.07901200652122498,
        'lsst_diaSource_ixyPSF': -0.040874190628528595,
        'lsst_diaSource_iyy': 0.16525594890117645,
        'lsst_diaSource_iyyPSF': 0.17802037298679352,
        'lsst_diaSource_midpointMjdTai': 61096.37656126988,
        'lsst_diaSource_parentDiaSourceId': 0,
        'lsst_diaSource_pixelFlags': False,
        'lsst_diaSource_pixelFlags_bad': False,
        'lsst_diaSource_pixelFlags_cr': False,
        'lsst_diaSource_pixelFlags_crCenter': False,
        'lsst_diaSource_pixelFlags_edge': False,
        'lsst_diaSource_pixelFlags_injected': False,
        'lsst_diaSource_pixelFlags_injectedCenter': False,
        'lsst_diaSource_pixelFlags_injected_template': False,
        'lsst_diaSource_pixelFlags_injected_templateCenter': False,
        'lsst_diaSource_pixelFlags_interpolated': False,
        'lsst_diaSource_pixelFlags_interpolatedCenter': False,
        'lsst_diaSource_pixelFlags_nodata': False,
        'lsst_diaSource_pixelFlags_nodataCenter': False,
        'lsst_diaSource_pixelFlags_offimage': False,
        'lsst_diaSource_pixelFlags_saturated': False,
        'lsst_diaSource_pixelFlags_saturatedCenter': False,
        'lsst_diaSource_pixelFlags_streak': False,
        'lsst_diaSource_pixelFlags_streakCenter': False,
        'lsst_diaSource_pixelFlags_suspect': False,
        'lsst_diaSource_pixelFlags_suspectCenter': False,
        'lsst_diaSource_psfChi2': 1625.6727294921875,
        'lsst_diaSource_psfFlux': 2459.348388671875,
        'lsst_diaSource_psfFluxErr': 122.97907257080078,
        'lsst_diaSource_psfFlux_flag': False,
        'lsst_diaSource_psfFlux_flag_edge': False,
        'lsst_diaSource_psfFlux_flag_noGoodPixels': False,
        'lsst_diaSource_psfNdata': 1681,
        'lsst_diaSource_ra': 186.6695224992812,
        'lsst_diaSource_raErr': 8.39069252833724e-06,
        'lsst_diaSource_ra_dec_Cov': -2.0313417614659102e-11,
        'lsst_diaSource_reliability': 0.9373371601104736,
        'lsst_diaSource_scienceFlux': 2442.664306640625,
        'lsst_diaSource_scienceFluxErr': 120.82659149169922,
        'lsst_diaSource_shape_flag': False,
        'lsst_diaSource_shape_flag_no_pixels': False,
        'lsst_diaSource_shape_flag_not_contained': False,
        'lsst_diaSource_shape_flag_parent_source': False,
        'lsst_diaSource_snr': 19.602617263793945,
        'lsst_diaSource_ssObjectId': 0,
        'lsst_diaSource_templateFlux': -23.538850784301758,
        'lsst_diaSource_templateFluxErr': 27.811079025268555,
        'lsst_diaSource_timeProcessedMjdTai': 61096.378328921826,
        'lsst_diaSource_trailAngle': -37.350406646728516,
        'lsst_diaSource_trailDec': 6.714929610232739,
        'lsst_diaSource_trailFlux': 2665.3583984375,
        'lsst_diaSource_trailFluxErr': 1.2446913719177246,
        'lsst_diaSource_trailLength': 1.3955764770507812,
        'lsst_diaSource_trailNdata': 0,
        'lsst_diaSource_trailRa': 186.66952053867425,
        'lsst_diaSource_trail_flag_edge': False,
        'lsst_diaSource_visit': 2026022401010,
        'lsst_diaSource_x': 3988.036865234375,
        'lsst_diaSource_xErr': 0.2330264002084732,
        'lsst_diaSource_y': 3932.305419921875,
        'lsst_diaSource_yErr': 0.14627179503440857,
        'lsst_observation_reason': 'alert_m49',
        'lsst_target_name': 'field_m49, lowdust',
    }

    # Randomly adjust the numerical values +/-10% to make the mock alerts unique
    alert = {}
    for key, val in example_alert.items():
        if isinstance(val, float):
            alert[key] = val * (1 + randint(-10, 10) / 100.0)
        else:
            alert[key] = val
    random_id = randint(1, alert['lsst_diaObject_diaObjectId'])
    alert['lsst_diaObject_diaObjectId'] = random_id
    alert['lsst_diaSource_diaSourceId'] = random_id
    return alert
