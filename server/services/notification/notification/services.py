"""Rendering and delivery.

Templates are plain format strings here rather than a template engine: the set
is small, the inputs come from our own events, and it keeps the render path
free of any expression evaluation.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Channel, Notification, Preference

logger = logging.getLogger(__name__)

# event_type -> (template_code, subject, body, channels)
TEMPLATES = {
    "payment.settled": (
        "TRANSFER_SETTLED", "Transfer completed",
        "Your transfer of {amount} to {beneficiary} is complete. Reference {reference}.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
    "payment.received": (
        "TRANSFER_RECEIVED", "Money received",
        # Addressed to the recipient, whose user_id this event carries -- the
        # other payment.* events all carry the payer's.
        "You received {amount} from {from_masked}. Reference {reference}.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
    "payment.approved": (
        "TRANSFER_APPROVED", "Transfer approved",
        "Your transfer of {amount} has been approved. Reference {reference}.",
        [Channel.IN_APP],
    ),
    "payment.blocked": (
        "TRANSFER_BLOCKED", "Transfer could not be completed",
        # No reason codes: telling a fraudster which rule fired tells them how
        # to evade it. Support has the detail if the customer is legitimate.
        "We couldn't complete your transfer (reference {reference}). "
        "Please contact support. No money has left your account.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
    "payment.review_required": (
        "TRANSFER_REVIEW", "Transfer under review",
        "Your transfer of {amount} (reference {reference}) is being reviewed for "
        "your security. The funds are on hold and we'll update you shortly.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
    "payment.returned": (
        "TRANSFER_RETURNED", "Transfer returned",
        "Your transfer of {amount} (reference {reference}) was returned and the "
        "money is back in your account.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
    "payment.failed": (
        "TRANSFER_FAILED", "Transfer failed",
        "Your transfer of {amount} (reference {reference}) could not be completed. "
        "No money has left your account.",
        [Channel.IN_APP],
    ),
    "payment.cancelled": (
        "TRANSFER_CANCELLED", "Transfer cancelled",
        "Your transfer of {amount} (reference {reference}) was cancelled.",
        [Channel.IN_APP],
    ),
    "account.opened": (
        "ACCOUNT_OPENED", "Your account is ready",
        "Welcome. Your account {account_number_masked} is open and ready to use.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
    "beneficiary.added": (
        "BENEFICIARY_ADDED", "New payee added",
        "A new payee was added to your account. If this wasn't you, contact us "
        "immediately.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
    "beneficiary.blocked": (
        "BENEFICIARY_BLOCKED", "Payee blocked",
        "A payee was blocked and can no longer be paid from your account.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
    "security.refresh_reuse_detected": (
        "SECURITY_ALERT", "Security alert: you've been signed out",
        "We detected unusual activity on your session and signed you out "
        "everywhere. Please sign in again.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
    "user.login_failed": (
        "LOGIN_FAILED", "Failed sign-in attempt",
        "There was a failed sign-in attempt on your account.",
        [Channel.IN_APP],
    ),
    # Administrative changes to an account. The person they were done *to* is
    # told, always -- an administrator acting alone on someone's access is
    # precisely what the affected customer needs to be able to challenge.
    "user.password_reset": (
        "PASSWORD_RESET", "Your password was reset",
        "An administrator reset the password on your account and signed you out "
        "everywhere. If you did not request this, contact us immediately.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
    "user.password_changed": (
        "PASSWORD_CHANGED", "Your password was changed",
        "Your password was changed and every other session was signed out. If "
        "this wasn't you, contact us immediately.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
    "user.status_changed": (
        "ACCOUNT_STATUS_CHANGED", "Your account status has changed",
        "Your account status is now {status}. Contact us if you were not "
        "expecting this.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
    "user.unlocked": (
        "ACCOUNT_UNLOCKED", "Your account has been unlocked",
        "Your account has been unlocked and you can sign in again.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
    "user.sessions_revoked": (
        "SESSIONS_REVOKED", "You've been signed out",
        "You were signed out on every device. Please sign in again.",
        [Channel.IN_APP],
    ),
    "schedule.failed": (
        "SCHEDULE_FAILED", "Standing order stopped",
        "Your recurring transfer has been stopped after repeated failures.",
        [Channel.IN_APP, Channel.EMAIL],
    ),
}


def _format_money(block) -> str:
    if isinstance(block, dict):
        return f"{Decimal(str(block.get('amount', '0')))} {block.get('currency', '')}".strip()
    return str(block or "")


def render(event_type: str, payload: dict) -> tuple[str, str, str, list[str]] | None:
    template = TEMPLATES.get(event_type)
    if template is None:
        return None
    code, subject, body, channels = template

    context = {
        "amount": _format_money(payload.get("amount")),
        "reference": payload.get("reference", ""),
        "beneficiary": payload.get("beneficiary_masked") or "the payee",
        "account_number_masked": payload.get("account_number_masked", ""),
        "from_masked": payload.get("from_masked") or "another account",
        "status": payload.get("status", ""),
    }
    try:
        rendered = body.format(**context)
    except KeyError:
        logger.exception("template %s is missing a field", code)
        rendered = body
    return code, subject, rendered, channels


def notify(*, event_id, event_type: str, user_id, payload: dict,
           correlation_id: str = "") -> list[Notification]:
    """Create one notification per enabled channel. Idempotent per event."""
    rendered = render(event_type, payload)
    if rendered is None or not user_id:
        return []
    code, subject, body, channels = rendered

    created = []
    for channel in channels:
        preference = Preference.objects.filter(
            user_id=user_id, category=code, channel=channel
        ).first()
        if preference and not preference.enabled:
            continue
        try:
            with transaction.atomic():
                created.append(
                    Notification.objects.create(
                        user_id=user_id, channel=channel, template_code=code,
                        subject=subject, body=body, source_event_id=event_id,
                        correlation_id=correlation_id,
                    )
                )
        except IntegrityError:
            # Redelivered event: this message already exists. Not an error.
            logger.debug("notification for event %s/%s already exists", event_id, channel)
    return created


def deliver(notification: Notification) -> bool:
    """Hand to the channel adapter.

    IN_APP is simply the row itself, which the SPA polls. EMAIL and SMS are
    simulated; a real provider is one class.
    """
    if notification.status == Notification.Status.SENT:
        return True
    try:
        if notification.channel != Channel.IN_APP:
            logger.info(
                "[%s] to user %s: %s", notification.channel,
                notification.user_id, notification.subject,
            )
        notification.status = Notification.Status.SENT
        notification.sent_at = timezone.now()
        notification.attempts += 1
        notification.save(update_fields=["status", "sent_at", "attempts"])
        return True
    except Exception as exc:
        notification.attempts += 1
        notification.last_error = repr(exc)[:2000]
        notification.status = (
            Notification.Status.FAILED if notification.attempts >= 5
            else Notification.Status.QUEUED
        )
        notification.save(update_fields=["attempts", "last_error", "status"])
        return False
