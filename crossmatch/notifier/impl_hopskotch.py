"""Hopskotch notification backend — publishes to SCiMMA Kafka via hop-client."""

from hop import Stream
from hop.auth import Auth
from django.conf import settings
from django.utils import timezone
from core.models import Notification
from core.log import get_logger

logger = get_logger(__name__)


def send_hopskotch_batch(notifications):
    """Publish a batch of notifications to Hopskotch via hop-client.

    Opens one Kafka connection per batch for efficiency. Each notification
    is published individually so failures are isolated.
    """
    url = f"{settings.HOPSKOTCH_BROKER_URL}/{settings.HOPSKOTCH_TOPIC}"
    if settings.HOPSKOTCH_USERNAME:
        auth = Auth(user=settings.HOPSKOTCH_USERNAME, password=settings.HOPSKOTCH_PASSWORD)
    else:
        auth = False
    stream = Stream(auth=auth)

    sent = 0
    failed = 0
    try:
        with stream.open(url, "w") as producer:
            for notif in notifications:
                try:
                    producer.write(notif.payload)
                    notif.state = Notification.State.SENT
                    notif.sent_at = timezone.now()
                    notif.attempts += 1
                    notif.save(update_fields=['state', 'sent_at', 'attempts', 'updated_at'])
                    sent += 1
                except Exception as err:
                    logger.error('Failed to publish notification',
                                 notification_id=notif.id, error=str(err))
                    notif.state = Notification.State.FAILED
                    notif.last_error = str(err)[:500]
                    notif.attempts += 1
                    notif.save(update_fields=['state', 'last_error', 'attempts', 'updated_at'])
                    failed += 1
    except Exception as err:
        logger.error('Failed to connect to Hopskotch broker',
                     url=url, error=str(err))
        for notif in notifications:
            if notif.state == Notification.State.PENDING:
                notif.state = Notification.State.FAILED
                notif.last_error = f"Connection error: {str(err)[:480]}"
                notif.attempts += 1
                notif.save(update_fields=['state', 'last_error', 'attempts', 'updated_at'])
                failed += 1

    logger.info('Hopskotch batch published',
                topic=settings.HOPSKOTCH_TOPIC, sent=sent, failed=failed)
