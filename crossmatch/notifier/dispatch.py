"""Notification destination handler registry."""

from notifier.impl_hopskotch import send_hopskotch_batch

DESTINATION_HANDLERS = {
    'hopskotch': send_hopskotch_batch,
}
