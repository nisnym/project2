"""Administrator user management, with segregation of duties.

The rule this module exists to enforce: **an administrator cannot both request
and apply a privilege change.** Everything that grants or restores access goes
through :class:`AdminChangeRequest` and waits for a different administrator.
Everything that takes access away applies immediately, because a control that
delays containment is a control that helps the attacker.

Two further limits, both of the kind that only hurt on the day they matter:

* An admin may not target themselves with a privileged change. Self-approval is
  refused at the approval step, but a request you cannot make is better than a
  request someone might approve by accident.
* The last active administrator cannot be demoted, closed, or locked. Locking
  yourself out of the only console that can unlock anything is unrecoverable
  without direct database access.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from platform_common.errors import Conflict, Forbidden, NotFound, ValidationFailed
from platform_common.events import EventEnvelope, publish
from platform_common.observability.context import get_correlation_id

from .models import (
    STAFF_ROLES,
    AdminChangeRequest,
    Device,
    RefreshToken,
    Role,
    User,
)

logger = logging.getLogger(__name__)

Action = AdminChangeRequest.Action
Status = AdminChangeRequest.Status

# Actions that grant or restore access, and therefore need a second pair of eyes.
DUAL_APPROVAL_ACTIONS = frozenset({
    Action.ROLE_CHANGE,
    Action.CREATE_STAFF,
    Action.UNLOCK,
    Action.ACTIVATE,
    Action.CLOSE,
    Action.PASSWORD_RESET,
})

TEMPORARY_PASSWORD_BYTES = 12


class SelfApproval(Forbidden):
    default_code = "SELF_APPROVAL"
    default_detail = "A change must be approved by a different administrator."


class SelfTarget(Forbidden):
    default_code = "SELF_TARGET"
    default_detail = "You cannot request a privileged change to your own account."


class LastAdministrator(Conflict):
    default_code = "LAST_ADMINISTRATOR"
    default_detail = (
        "This is the last active administrator. Promote another administrator first."
    )


# --------------------------------------------------------------------- events


def _emit(event_type: str, *, aggregate_id: str, actor: dict, payload: dict) -> None:
    publish(
        EventEnvelope(
            event_type=event_type,
            aggregate_type="user",
            aggregate_id=str(aggregate_id),
            sequence=0,
            producer="identity",
            correlation_id=get_correlation_id() or "",
            actor=actor,
            payload=payload,
        )
    )


def _actor(admin) -> dict:
    return {"type": "admin", "id": str(admin.id)}


# ---------------------------------------------------------------------- reads


def list_users(*, q: str = "", role: str = "", status: str = "",
               limit: int = 50, offset: int = 0) -> tuple[list[User], int]:
    query = User.objects.all()

    if q:
        query = query.filter(Q(email__icontains=q) | Q(full_name__icontains=q))
    if role:
        query = query.filter(role__in=role.split(","))
    if status:
        query = query.filter(status__in=status.split(","))

    total = query.count()
    rows = list(query.order_by("-created_at")[offset: offset + limit])
    return rows, total


def active_sessions(user: User) -> int:
    return RefreshToken.objects.filter(
        user=user, revoked_at__isnull=True, replaced_by__isnull=True,
        expires_at__gt=timezone.now(),
    ).count()


def _admin_count(*, excluding=None) -> int:
    query = User.objects.filter(role=Role.ADMIN, status=User.Status.ACTIVE)
    if excluding is not None:
        query = query.exclude(pk=excluding)
    return query.count()


def _guard_last_admin(user: User) -> None:
    """Refuse a change that would leave nobody able to administer the estate."""
    if user.role != Role.ADMIN or user.status != User.Status.ACTIVE:
        return
    if _admin_count(excluding=user.pk) == 0:
        raise LastAdministrator()


# ----------------------------------------------------- immediate (containment)


def lock_user(user: User, *, admin, reason: str = "") -> User:
    """Suspend an account now. Single administrator, by design."""
    _guard_last_admin(user)

    with transaction.atomic():
        user.status = User.Status.LOCKED
        user.locked_until = timezone.now() + timedelta(days=3650)
        user.save(update_fields=["status", "locked_until"])
        revoked = _revoke_sessions(user)
        _emit(
            "user.status_changed",
            aggregate_id=user.id, actor=_actor(admin),
            payload={"user_id": str(user.id), "email": user.email,
                     "status": user.status, "previous_status": User.Status.ACTIVE,
                     "sessions_revoked": revoked, "reason": reason,
                     "requires_approval": False},
        )
    logger.warning("admin %s locked user %s", admin.id, user.email)
    return user


def revoke_sessions(user: User, *, admin, reason: str = "") -> int:
    """Sign the user out everywhere. Access tokens live 15 minutes; the refresh
    families die immediately, so the blast radius is one token lifetime."""
    with transaction.atomic():
        revoked = _revoke_sessions(user)
        _emit(
            "user.sessions_revoked",
            aggregate_id=user.id, actor=_actor(admin),
            payload={"user_id": str(user.id), "email": user.email,
                     "sessions_revoked": revoked, "reason": reason},
        )
    return revoked


def _revoke_sessions(user: User) -> int:
    return RefreshToken.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )


def update_profile(user: User, *, admin, full_name=None, phone=None) -> User:
    """Non-privileged correction of contact details.

    Nothing here grants access, so it does not need a second approver -- but it
    is still audited, because "who changed the phone number just before the
    password reset" is exactly the question a fraud investigation asks.
    """
    changed = {}
    if full_name is not None and full_name != user.full_name:
        changed["full_name"] = {"from": user.full_name, "to": full_name}
        user.full_name = full_name
    if phone is not None and phone != user.phone:
        changed["phone"] = {"from": user.phone, "to": phone}
        user.phone = phone

    if not changed:
        raise ValidationFailed("No profile fields were changed.")

    with transaction.atomic():
        user.save(update_fields=list(changed))
        _emit(
            "user.profile_updated",
            aggregate_id=user.id, actor=_actor(admin),
            payload={"user_id": str(user.id), "email": user.email, "changed": changed},
        )
    return user


# ------------------------------------------------------- maker: raise a request


def request_change(*, action: str, admin, target: User | None = None,
                   payload: dict | None = None, reason: str = "") -> AdminChangeRequest:
    """Stage a privileged change. Applies nothing."""
    if action not in DUAL_APPROVAL_ACTIONS:
        raise ValidationFailed(f"{action} is not a dual-approval action.")

    payload = dict(payload or {})

    if target is not None and str(target.id) == str(admin.id):
        raise SelfTarget()

    _validate_request(action, target, payload)

    try:
        with transaction.atomic():
            change = AdminChangeRequest.objects.create(
                action=action,
                target_user=target,
                target_email=(target.email if target else payload.get("email", "")),
                payload=payload,
                reason=reason[:200],
                requested_by=admin.id,
                requested_by_email=getattr(admin, "email", ""),
                correlation_id=get_correlation_id() or "",
            )
            _emit(
                "admin.change_requested",
                aggregate_id=(target.id if target else change.id),
                actor=_actor(admin),
                payload={
                    "change_request_id": str(change.id),
                    "action": action,
                    "target_user_id": str(target.id) if target else None,
                    "target_email": change.target_email,
                    "requested_by": str(admin.id),
                    "detail": _public_payload(action, payload),
                    "reason": change.reason,
                },
            )
    except IntegrityError as exc:
        raise Conflict(
            "There is already an open request for this action on this user.",
            detail={"action": action},
        ) from exc

    logger.info("admin %s requested %s on %s", admin.id, action, change.target_email)
    return change


def _validate_request(action: str, target: User | None, payload: dict) -> None:
    """Reject at request time what could never be approved.

    Queueing a change that is guaranteed to fail wastes the approver's attention,
    which is the scarce resource the whole control depends on.
    """
    if action == Action.CREATE_STAFF:
        email = (payload.get("email") or "").strip().lower()
        role = payload.get("role")
        if not email:
            raise ValidationFailed("An email address is required.")
        if role not in STAFF_ROLES:
            raise ValidationFailed(
                "A staff account must be one of FRAUD_ANALYST, OPS or ADMIN.",
                detail={"allowed": sorted(STAFF_ROLES)},
            )
        if User.objects.filter(email__iexact=email).exists():
            raise Conflict("An account with that email already exists.")
        payload["email"] = email
        return

    if target is None:
        raise ValidationFailed(f"{action} needs a target user.")

    if action == Action.ROLE_CHANGE:
        role = payload.get("role")
        if role not in Role.values:
            raise ValidationFailed("Unknown role.", detail={"allowed": list(Role.values)})
        if role == target.role:
            raise ValidationFailed(f"{target.email} already has the role {role}.")
        if target.role == Role.ADMIN:
            _guard_last_admin(target)

    elif action == Action.UNLOCK:
        if not target.is_locked and target.failed_logins == 0:
            raise ValidationFailed(f"{target.email} is not locked.")

    elif action == Action.ACTIVATE:
        if target.status == User.Status.ACTIVE:
            raise ValidationFailed(f"{target.email} is already active.")

    elif action == Action.CLOSE:
        if target.status == User.Status.CLOSED:
            raise ValidationFailed(f"{target.email} is already closed.")
        _guard_last_admin(target)


def _public_payload(action: str, payload: dict) -> dict:
    """The parts of a request safe to put in an event and show in a queue.

    Never the temporary password: the audit log is readable by every member of
    staff with the audit scope, and a credential in it is a credential leaked.
    """
    return {k: v for k, v in payload.items() if k not in ("temporary_password",)}


def withdraw(change_id, *, admin) -> AdminChangeRequest:
    with transaction.atomic():
        change = _open_request(change_id)
        if str(change.requested_by) != str(admin.id):
            raise Forbidden("Only the requester may withdraw a change.")
        change.status = Status.WITHDRAWN
        change.decided_at = timezone.now()
        change.save(update_fields=["status", "decided_at"])
        _emit(
            "admin.change_withdrawn",
            aggregate_id=change.target_user_id or change.id, actor=_actor(admin),
            payload={"change_request_id": str(change.id), "action": change.action,
                     "target_email": change.target_email},
        )
    return change


# ---------------------------------------------------- checker: decide a request


def _open_request(change_id) -> AdminChangeRequest:
    change = AdminChangeRequest.objects.select_for_update().filter(pk=change_id).first()
    if change is None:
        raise NotFound("Change request not found.")
    if not change.is_open:
        raise Conflict(
            f"This request has already been {change.status.lower()}.",
            detail={"status": change.status},
        )
    return change


def reject(change_id, *, admin, reason: str = "") -> AdminChangeRequest:
    with transaction.atomic():
        change = _open_request(change_id)
        if str(change.requested_by) == str(admin.id):
            raise SelfApproval("You cannot decide your own request; withdraw it instead.")

        change.status = Status.REJECTED
        change.decided_by = admin.id
        change.decided_by_email = getattr(admin, "email", "")
        change.decided_at = timezone.now()
        change.decision_reason = reason[:200]
        change.save(update_fields=["status", "decided_by", "decided_by_email",
                                   "decided_at", "decision_reason"])
        _emit(
            "admin.change_rejected",
            aggregate_id=change.target_user_id or change.id, actor=_actor(admin),
            payload={"change_request_id": str(change.id), "action": change.action,
                     "target_email": change.target_email,
                     "requested_by": str(change.requested_by),
                     "rejected_by": str(admin.id), "reason": change.decision_reason},
        )
    return change


def approve(change_id, *, admin) -> tuple[AdminChangeRequest, dict]:
    """Approve and apply, in one transaction.

    Returns ``(change_request, secrets)``. ``secrets`` carries a temporary
    password for the actions that mint one -- shown to the approving admin
    exactly once and never stored in plaintext, published, or audited.
    """
    with transaction.atomic():
        change = _open_request(change_id)

        # The whole point of the control.
        if str(change.requested_by) == str(admin.id):
            raise SelfApproval()

        try:
            revealed = _apply(change, admin=admin)
        except Exception as exc:
            # Park it as FAILED rather than rolling back to PENDING: an approval
            # that could not be applied is a fact the queue must show, not an
            # invitation to click again and hope.
            logger.exception("could not apply change request %s", change.id)
            change.status = Status.FAILED
            change.decided_by = admin.id
            change.decided_by_email = getattr(admin, "email", "")
            change.decided_at = timezone.now()
            change.error = repr(exc)[:2000]
            change.save(update_fields=["status", "decided_by", "decided_by_email",
                                       "decided_at", "error"])
            raise

        change.status = Status.APPLIED
        change.decided_by = admin.id
        change.decided_by_email = getattr(admin, "email", "")
        change.decided_at = timezone.now()
        change.save(update_fields=["status", "decided_by", "decided_by_email",
                                   "decided_at"])

        _emit(
            "admin.change_applied",
            aggregate_id=change.target_user_id or change.id, actor=_actor(admin),
            payload={
                "change_request_id": str(change.id), "action": change.action,
                "target_user_id": str(change.target_user_id) if change.target_user_id else None,
                "target_email": change.target_email,
                "requested_by": str(change.requested_by),
                "approved_by": str(admin.id),
                "detail": _public_payload(change.action, change.payload),
            },
        )
    return change, revealed


def _apply(change: AdminChangeRequest, *, admin) -> dict:
    """Perform the approved mutation. Runs inside the approval transaction."""
    action = change.action

    if action == Action.CREATE_STAFF:
        return _apply_create_staff(change, admin=admin)

    user = User.objects.select_for_update().filter(pk=change.target_user_id).first()
    if user is None:
        raise NotFound("The target user no longer exists.")

    if action == Action.ROLE_CHANGE:
        return _apply_role_change(change, user, admin=admin)
    if action == Action.UNLOCK:
        return _apply_unlock(change, user, admin=admin)
    if action == Action.ACTIVATE:
        return _apply_status(change, user, User.Status.ACTIVE, admin=admin)
    if action == Action.CLOSE:
        return _apply_status(change, user, User.Status.CLOSED, admin=admin)
    if action == Action.PASSWORD_RESET:
        return _apply_password_reset(change, user, admin=admin)

    raise ValidationFailed(f"Unsupported action {action}.")


def _apply_create_staff(change: AdminChangeRequest, *, admin) -> dict:
    payload = change.payload
    email = payload["email"]
    if User.objects.filter(email__iexact=email).exists():
        raise Conflict("An account with that email already exists.")

    temporary = secrets.token_urlsafe(TEMPORARY_PASSWORD_BYTES)
    user = User(
        email=email,
        full_name=payload.get("full_name", ""),
        phone=payload.get("phone", ""),
        role=payload["role"],
        status=User.Status.ACTIVE,
        must_change_password=True,
    )
    user.set_password(temporary)
    user.save()

    change.target_user = user
    change.save(update_fields=["target_user"])

    _emit(
        "user.created_by_admin",
        aggregate_id=user.id, actor=_actor(admin),
        payload={"user_id": str(user.id), "email": user.email, "role": user.role,
                 "requested_by": str(change.requested_by),
                 "approved_by": str(admin.id)},
    )
    return {"user_id": str(user.id), "temporary_password": temporary}


def _apply_role_change(change: AdminChangeRequest, user: User, *, admin) -> dict:
    previous = user.role
    new_role = change.payload["role"]

    if previous == Role.ADMIN and new_role != Role.ADMIN:
        _guard_last_admin(user)

    user.role = new_role
    user.save(update_fields=["role"])

    # The old access token carries the old role in its claims and stays valid
    # for up to 15 minutes. Killing the refresh families forces a fresh sign-in,
    # so a demotion takes effect on the next token rather than the next hour.
    revoked = _revoke_sessions(user)

    _emit(
        "user.role_changed",
        aggregate_id=user.id, actor=_actor(admin),
        payload={"user_id": str(user.id), "email": user.email,
                 "previous_role": previous, "role": new_role,
                 "sessions_revoked": revoked,
                 "requested_by": str(change.requested_by),
                 "approved_by": str(admin.id)},
    )
    return {}


def _apply_unlock(change: AdminChangeRequest, user: User, *, admin) -> dict:
    user.locked_until = None
    user.failed_logins = 0
    if user.status == User.Status.LOCKED:
        user.status = User.Status.ACTIVE
    user.save(update_fields=["locked_until", "failed_logins", "status"])

    _emit(
        "user.unlocked",
        aggregate_id=user.id, actor=_actor(admin),
        payload={"user_id": str(user.id), "email": user.email, "status": user.status,
                 "requested_by": str(change.requested_by),
                 "approved_by": str(admin.id)},
    )
    return {}


def _apply_status(change: AdminChangeRequest, user: User, status: str, *, admin) -> dict:
    previous = user.status
    if status != User.Status.ACTIVE:
        # Re-checked at approval, not only at request: the estate may have lost
        # its other administrators in the interval, and an approval queue is
        # exactly where that interval gets long.
        _guard_last_admin(user)

    user.status = status
    if status == User.Status.ACTIVE:
        user.locked_until = None
        user.failed_logins = 0
        user.save(update_fields=["status", "locked_until", "failed_logins"])
        revoked = 0
    else:
        user.save(update_fields=["status"])
        revoked = _revoke_sessions(user)

    _emit(
        "user.status_changed",
        aggregate_id=user.id, actor=_actor(admin),
        payload={"user_id": str(user.id), "email": user.email, "status": status,
                 "previous_status": previous, "sessions_revoked": revoked,
                 "requested_by": str(change.requested_by),
                 "approved_by": str(admin.id), "requires_approval": True},
    )
    return {}


def _apply_password_reset(change: AdminChangeRequest, user: User, *, admin) -> dict:
    temporary = secrets.token_urlsafe(TEMPORARY_PASSWORD_BYTES)
    user.set_password(temporary)
    user.must_change_password = True
    user.failed_logins = 0
    user.locked_until = None
    user.save(update_fields=["password_hash", "must_change_password",
                             "failed_logins", "locked_until"])
    revoked = _revoke_sessions(user)

    _emit(
        "user.password_reset",
        aggregate_id=user.id, actor=_actor(admin),
        payload={"user_id": str(user.id), "email": user.email,
                 "sessions_revoked": revoked,
                 "requested_by": str(change.requested_by),
                 "approved_by": str(admin.id)},
    )
    return {"temporary_password": temporary}


# --------------------------------------------------------------------- queues


def pending_requests(*, limit: int = 100, status: str = Status.PENDING):
    query = AdminChangeRequest.objects.all()
    if status:
        query = query.filter(status__in=status.split(","))
    return list(query.select_related("target_user")[:limit])


def stats() -> dict:
    from django.db.models import Count

    return {
        "by_role": {
            row["role"]: row["count"]
            for row in User.objects.values("role").annotate(count=Count("id"))
        },
        "by_status": {
            row["status"]: row["count"]
            for row in User.objects.values("status").annotate(count=Count("id"))
        },
        "locked_now": User.objects.filter(locked_until__gt=timezone.now()).count(),
        "pending_approvals": AdminChangeRequest.objects.filter(
            status=Status.PENDING
        ).count(),
        "devices": Device.objects.count(),
    }
