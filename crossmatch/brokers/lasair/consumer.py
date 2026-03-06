"""Lasair Kafka alert consumer — deferred to future work.

Lasair broker: kafka.lsst.ac.uk:9092
Topic: lasair_366SCiMMA_reliability_moderate
Auth: no credentials required for ingest path
Filter: reliability_moderate — latestR > 0.7, nDiaSources >= 1, age < 1 day
"""
from core.log import get_logger

logger = get_logger(__name__)


def consume_alerts():
    """Consume alerts from the Lasair Kafka broker."""
    raise NotImplementedError("Lasair consumer not yet implemented")
