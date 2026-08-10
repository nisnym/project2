"""Administrator HTTP surface: authorise, parse, delegate, respond.

Every route here is ADMIN-only. Staff roles that need to *see* a customer do so
through their own consoles, which show a transaction or a case -- not the user
record, which is the densest concentration of personal data in the estate.
"""

from __future__ import annotations

from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from platform_common.auth import IsAdmin
from platform_common.auth.authentication import JWTAuthentication
from platform_common.errors import NotFound, ValidationFailed

from . import admin_services
from .models import STAFF_ROLES, AdminChangeRequest, Device, Role, User
from .serializers import (
    CreateStaffSerializer,
    ProfileUpdateSerializer,
    RoleChangeSerializer,
    UserActionSerializer,
)

ADMIN = [IsAdmin]
Action = AdminChangeRequest.Action


# --------------------------------------------------------------------- shapes


def _user_json(user: User, *, detail: bool = False) -> dict:
    body = {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "phone": user.phone,
        "role": user.role,
        "status": user.status,
        "mfa_enabled": user.mfa_enabled,
        "must_change_password": user.must_change_password,
        "is_locked": user.is_locked,
        "failed_logins": user.failed_logins,
        "locked_until": user.locked_until.isoformat() if user.locked_until else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat(),
    }
    if not detail:
        return body

    body["active_sessions"] = admin_services.active_sessions(user)
    body["devices"] = [
        {
            "id": str(device.id),
            "user_agent": device.user_agent,
            "last_ip_country": device.last_ip_country,
            "trusted": device.trusted,
            "first_seen": device.first_seen.isoformat(),
            "last_seen": device.last_seen.isoformat(),
        }
        for device in Device.objects.filter(user=user).order_by("-last_seen")[:20]
    ]
    body["open_requests"] = [
        _change_json(change)
        for change in AdminChangeRequest.objects.filter(
            target_user=user, status=AdminChangeRequest.Status.PENDING
        )
    ]
    return body


def _change_json(change: AdminChangeRequest) -> dict:
    return {
        "id": str(change.id),
        "action": change.action,
        "action_label": change.get_action_display(),
        "status": change.status,
        "target_user_id": str(change.target_user_id) if change.target_user_id else None,
        "target_email": change.target_email,
        # Never the temporary password -- see admin_services._public_payload.
        "payload": admin_services._public_payload(change.action, change.payload),
        "reason": change.reason,
        "requested_by": str(change.requested_by),
        "requested_by_email": change.requested_by_email,
        "requested_at": change.requested_at.isoformat(),
        "decided_by": str(change.decided_by) if change.decided_by else None,
        "decided_by_email": change.decided_by_email,
        "decided_at": change.decided_at.isoformat() if change.decided_at else None,
        "decision_reason": change.decision_reason,
        "error": change.error,
        "correlation_id": change.correlation_id,
    }


def _target(user_id) -> User:
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        raise NotFound("User not found.")
    return user


# ---------------------------------------------------------------------- users


@api_view(["GET", "POST"])
@authentication_classes([JWTAuthentication])
@permission_classes(ADMIN)
def users(request):
    """List and search users, or raise a request to create a staff account."""
    if request.method == "GET":
        limit = min(int(request.query_params.get("limit", 50)), 200)
        offset = max(int(request.query_params.get("offset", 0)), 0)
        rows, total = admin_services.list_users(
            q=request.query_params.get("q", "").strip(),
            role=request.query_params.get("role", ""),
            status=request.query_params.get("status", ""),
            limit=limit,
            offset=offset,
        )
        return Response({
            "results": [_user_json(user) for user in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
            "roles": list(Role.values),
            "staff_roles": sorted(STAFF_ROLES),
            "statuses": list(User.Status.values),
        })

    payload = CreateStaffSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    data = dict(payload.validated_data)
    reason = data.pop("reason", "")

    change = admin_services.request_change(
        action=Action.CREATE_STAFF, admin=request.user, payload=data, reason=reason,
    )
    return Response(_change_json(change), status=202)


@api_view(["GET", "PATCH"])
@authentication_classes([JWTAuthentication])
@permission_classes(ADMIN)
def user_detail(request, user_id):
    user = _target(user_id)

    if request.method == "PATCH":
        payload = ProfileUpdateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        user = admin_services.update_profile(
            user, admin=request.user, **payload.validated_data
        )

    return Response(_user_json(user, detail=True))


# The action a caller names, mapped to how it is carried out. Anything in
# DUAL_APPROVAL_ACTIONS is staged; the rest applies now.
IMMEDIATE_ACTIONS = {"LOCK", "REVOKE_SESSIONS"}


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes(ADMIN)
def user_action(request, user_id):
    """Take, or ask for, an action on one user.

    Containment (LOCK, REVOKE_SESSIONS) returns 200 and is already done.
    Anything that grants or restores access returns 202 with a change request
    that a second administrator must approve.
    """
    payload = UserActionSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    action = payload.validated_data["action"]
    reason = payload.validated_data.get("reason", "")

    user = _target(user_id)

    if action == "LOCK":
        admin_services.lock_user(user, admin=request.user, reason=reason)
        return Response({"applied": True, "user": _user_json(user, detail=True)})

    if action == "REVOKE_SESSIONS":
        revoked = admin_services.revoke_sessions(user, admin=request.user, reason=reason)
        return Response({
            "applied": True, "sessions_revoked": revoked,
            "user": _user_json(user, detail=True),
        })

    body = {}
    if action == Action.ROLE_CHANGE:
        role = RoleChangeSerializer(data=request.data)
        role.is_valid(raise_exception=True)
        body = {"role": role.validated_data["role"]}

    change = admin_services.request_change(
        action=action, admin=request.user, target=user, payload=body, reason=reason,
    )
    return Response(
        {"applied": False, "requires_approval": True, "change_request": _change_json(change)},
        status=202,
    )


# ----------------------------------------------------------- approvals queue


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes(ADMIN)
def change_requests(request):
    rows = admin_services.pending_requests(
        limit=min(int(request.query_params.get("limit", 100)), 200),
        status=request.query_params.get("status", AdminChangeRequest.Status.PENDING),
    )
    return Response({
        "results": [_change_json(change) for change in rows],
        # So the console can grey out the ones this admin raised rather than
        # letting them click through to a 403.
        "viewer_id": str(request.user.id),
    })


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes(ADMIN)
def approve_change(request, change_id):
    change, revealed = admin_services.approve(change_id, admin=request.user)
    body = {"change_request": _change_json(change), "applied": True}
    if revealed:
        # Shown once, to the approver, and never persisted in the clear. There
        # is deliberately no endpoint to read it back.
        body["one_time_secret"] = revealed
    return Response(body)


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes(ADMIN)
def reject_change(request, change_id):
    reason = str(request.data.get("reason", "")).strip()
    if not reason:
        raise ValidationFailed("A rejection needs a reason.")
    change = admin_services.reject(change_id, admin=request.user, reason=reason)
    return Response({"change_request": _change_json(change), "applied": False})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes(ADMIN)
def withdraw_change(request, change_id):
    change = admin_services.withdraw(change_id, admin=request.user)
    return Response({"change_request": _change_json(change), "applied": False})


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes(ADMIN)
def admin_stats(request):
    return Response(admin_services.stats())
