"""Notification dispatch — superseded by Celery Beat task dispatch_notifications.

The dispatch_notifications periodic task in tasks/schedule.py polls for pending
Notification rows and dispatches them via destination handlers. See
notifier/dispatch.py for the handler registry.
"""
