"""Administrator user management and its segregation of duties.

The interesting tests here are the refusals. Anyone can write an admin console
that changes a role; the controls that matter are the ones that stop a single
compromised administrator account from quietly granting itself, or a colleague,
whatever it wants -- and the ones that stop a well-meaning admin from locking
the entire estate out of its own console.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from identity import admin_services, services
from identity.models import AdminChangeRequest, RefreshToken, Role, User
from platform_common.errors import Conflict, Forbidden, NotFound, ValidationFailed

pytestmark = pytest.mark.django_db

Action = AdminChangeRequest.Action
Status = AdminChangeRequest.Status


@pytest.fixture(autouse=True)
def no_outbound_events(monkeypatch):
    monkeypatch.setattr(
        "platform_common.events.publisher._enqueue_relay", lambda outbox_id: None
    )


def make_user(email, role=Role.CUSTOMER, status=User.Status.ACTIVE) -> User:
    user = User(email=email, full_name=email.split("@")[0], role=role, status=status)
    user.set_password("correct-horse-battery")
    user.save()
    return user


def principal(user: User) -> SimpleNamespace:
    """The shape a view hands the service layer: a Principal, not a User."""
    return SimpleNamespace(id=str(user.id), email=user.email, role=user.role)


@pytest.fixture
def maker():
    return make_user("maker@indbank.test", Role.ADMIN)


@pytest.fixture
def checker():
    return make_user("checker@indbank.test", Role.ADMIN)


@pytest.fixture
def customer():
    return make_user("asha@indbank.test", Role.CUSTOMER)


class TestSegregationOfDuties:
    def test_a_role_change_does_nothing_until_approved(self, maker, checker, customer):
        admin_services.request_change(
            action=Action.ROLE_CHANGE, admin=principal(maker), target=customer,
            payload={"role": Role.OPS}, reason="joining the ops rota",
        )
        customer.refresh_from_db()
        assert customer.role == Role.CUSTOMER

    def test_approval_by_a_second_admin_applies_it(self, maker, checker, customer):
        change = admin_services.request_change(
            action=Action.ROLE_CHANGE, admin=principal(maker), target=customer,
            payload={"role": Role.OPS},
        )
        admin_services.approve(change.id, admin=principal(checker))

        customer.refresh_from_db()
        change.refresh_from_db()
        assert customer.role == Role.OPS
        assert change.status == Status.APPLIED
        assert change.decided_by_email == checker.email

    def test_the_requester_cannot_approve_their_own_request(self, maker, customer):
        change = admin_services.request_change(
            action=Action.ROLE_CHANGE, admin=principal(maker), target=customer,
            payload={"role": Role.ADMIN},
        )
        with pytest.raises(Forbidden) as caught:
            admin_services.approve(change.id, admin=principal(maker))

        assert caught.value.code == "SELF_APPROVAL"
        customer.refresh_from_db()
        assert customer.role == Role.CUSTOMER

    def test_the_requester_cannot_reject_their_own_request_either(self, maker, customer):
        """Otherwise a maker could bury an inconvenient request rather than
        withdrawing it on the record."""
        change = admin_services.request_change(
            action=Action.CLOSE, admin=principal(maker), target=customer,
        )
        with pytest.raises(Forbidden):
            admin_services.reject(change.id, admin=principal(maker), reason="never mind")

    def test_a_maker_may_withdraw_their_own_request(self, maker, customer):
        change = admin_services.request_change(
            action=Action.CLOSE, admin=principal(maker), target=customer,
        )
        admin_services.withdraw(change.id, admin=principal(maker))

        change.refresh_from_db()
        assert change.status == Status.WITHDRAWN

    def test_an_admin_cannot_target_themselves(self, maker):
        with pytest.raises(Forbidden) as caught:
            admin_services.request_change(
                action=Action.ROLE_CHANGE, admin=principal(maker), target=maker,
                payload={"role": Role.CUSTOMER},
            )
        assert caught.value.code == "SELF_TARGET"

    def test_a_decided_request_cannot_be_decided_again(self, maker, checker, customer):
        change = admin_services.request_change(
            action=Action.CLOSE, admin=principal(maker), target=customer,
        )
        admin_services.approve(change.id, admin=principal(checker))

        with pytest.raises(Conflict):
            admin_services.approve(change.id, admin=principal(checker))

    def test_two_open_requests_for_the_same_action_are_refused(
        self, maker, checker, customer
    ):
        """Two admins independently queueing the same grant must not become two
        approvals of it."""
        admin_services.request_change(
            action=Action.ROLE_CHANGE, admin=principal(maker), target=customer,
            payload={"role": Role.OPS},
        )
        with pytest.raises(Conflict):
            admin_services.request_change(
                action=Action.ROLE_CHANGE, admin=principal(checker), target=customer,
                payload={"role": Role.ADMIN},
            )


class TestContainmentIsImmediate:
    """Taking access away never waits for a quorum.

    A control that delays containment is a control that helps the attacker: if
    an account takeover is detected at 2am, locking the account cannot depend on
    finding a second administrator.
    """

    def test_lock_applies_at_once_and_kills_sessions(self, maker, customer):
        services.login(email=customer.email, password="correct-horse-battery")
        assert admin_services.active_sessions(customer) == 1

        admin_services.lock_user(customer, admin=principal(maker), reason="takeover")

        customer.refresh_from_db()
        assert customer.status == User.Status.LOCKED
        assert admin_services.active_sessions(customer) == 0
        assert AdminChangeRequest.objects.count() == 0

    def test_revoking_sessions_applies_at_once(self, maker, customer):
        services.login(email=customer.email, password="correct-horse-battery")

        revoked = admin_services.revoke_sessions(customer, admin=principal(maker))

        assert revoked == 1
        assert admin_services.active_sessions(customer) == 0

    def test_a_locked_customer_cannot_sign_in(self, maker, customer):
        admin_services.lock_user(customer, admin=principal(maker))

        with pytest.raises(Forbidden):
            services.login(email=customer.email, password="correct-horse-battery")

    def test_restoring_access_does_need_approval(self, maker, checker, customer):
        """The mirror image: unlocking is a grant, so it waits."""
        admin_services.lock_user(customer, admin=principal(maker))

        change = admin_services.request_change(
            action=Action.ACTIVATE, admin=principal(maker), target=customer,
        )
        customer.refresh_from_db()
        assert customer.status == User.Status.LOCKED

        admin_services.approve(change.id, admin=principal(checker))
        customer.refresh_from_db()
        assert customer.status == User.Status.ACTIVE


class TestLastAdministrator:
    def test_the_last_admin_cannot_be_demoted(self, maker, customer):
        """maker is the only ADMIN here. Demoting them leaves nobody who can
        approve promoting anyone back, which is unrecoverable without database
        access -- so the request is refused before it ever reaches a queue."""
        with pytest.raises(Conflict) as caught:
            admin_services.request_change(
                action=Action.ROLE_CHANGE, admin=principal(customer), target=maker,
                payload={"role": Role.CUSTOMER},
            )

        assert caught.value.code == "LAST_ADMINISTRATOR"
        maker.refresh_from_db()
        assert maker.role == Role.ADMIN

    def test_the_last_admin_cannot_be_locked(self, maker, customer):
        with pytest.raises(Conflict):
            admin_services.lock_user(maker, admin=principal(customer))

    def test_demotion_is_fine_while_another_admin_remains(
        self, maker, checker, customer
    ):
        change = admin_services.request_change(
            action=Action.ROLE_CHANGE, admin=principal(customer), target=maker,
            payload={"role": Role.OPS},
        )
        admin_services.approve(change.id, admin=principal(checker))

        maker.refresh_from_db()
        assert maker.role == Role.OPS

    def test_the_guard_is_rechecked_at_approval_not_only_at_request(
        self, maker, checker
    ):
        """An approval queue is exactly where the gap between request and apply
        gets long enough for the world to change underneath it."""
        third = make_user("third@indbank.test", Role.ADMIN)

        # Legitimate when raised: maker is one of three administrators.
        change = admin_services.request_change(
            action=Action.ROLE_CHANGE, admin=principal(checker), target=maker,
            payload={"role": Role.OPS},
        )
        # Both other administrators leave while it sits in the queue.
        User.objects.filter(pk__in=[checker.pk, third.pk]).update(
            status=User.Status.CLOSED
        )

        with pytest.raises(Conflict) as caught:
            admin_services.approve(change.id, admin=principal(third))

        assert caught.value.code == "LAST_ADMINISTRATOR"
        maker.refresh_from_db()
        assert maker.role == Role.ADMIN


class TestStaffCreation:
    def test_a_staff_account_appears_only_after_approval(self, maker, checker):
        change = admin_services.request_change(
            action=Action.CREATE_STAFF, admin=principal(maker),
            payload={"email": "newops@indbank.test", "role": Role.OPS,
                     "full_name": "Kabir Shah"},
        )
        assert not User.objects.filter(email="newops@indbank.test").exists()

        _, revealed = admin_services.approve(change.id, admin=principal(checker))

        created = User.objects.get(email="newops@indbank.test")
        assert created.role == Role.OPS
        assert created.status == User.Status.ACTIVE
        assert created.must_change_password is True
        assert revealed["temporary_password"]

    def test_the_temporary_password_works_once_and_is_never_stored(
        self, maker, checker
    ):
        change = admin_services.request_change(
            action=Action.CREATE_STAFF, admin=principal(maker),
            payload={"email": "newops@indbank.test", "role": Role.OPS},
        )
        _, revealed = admin_services.approve(change.id, admin=principal(checker))
        temporary = revealed["temporary_password"]

        result = services.login(email="newops@indbank.test", password=temporary)
        assert result["must_change_password"] is True

        # Not in the request row, so not in any queue, event or audit entry.
        change.refresh_from_db()
        assert temporary not in str(change.payload)

    def test_a_customer_cannot_be_minted_this_way(self, maker):
        """Customers arrive through onboarding, which opens an account and runs
        KYC. A staff-created customer would have neither."""
        with pytest.raises(ValidationFailed):
            admin_services.request_change(
                action=Action.CREATE_STAFF, admin=principal(maker),
                payload={"email": "someone@indbank.test", "role": Role.CUSTOMER},
            )

    def test_a_duplicate_email_is_refused_at_request_time(self, maker, customer):
        with pytest.raises(Conflict):
            admin_services.request_change(
                action=Action.CREATE_STAFF, admin=principal(maker),
                payload={"email": customer.email, "role": Role.OPS},
            )


class TestPasswordReset:
    def test_reset_needs_approval_and_signs_the_user_out(
        self, maker, checker, customer
    ):
        services.login(email=customer.email, password="correct-horse-battery")

        change = admin_services.request_change(
            action=Action.PASSWORD_RESET, admin=principal(maker), target=customer,
        )
        _, revealed = admin_services.approve(change.id, admin=principal(checker))

        customer.refresh_from_db()
        assert customer.must_change_password is True
        assert admin_services.active_sessions(customer) == 0
        assert services.login(
            email=customer.email, password=revealed["temporary_password"]
        )

    def test_changing_the_password_clears_the_flag_and_other_sessions(
        self, maker, checker, customer
    ):
        change = admin_services.request_change(
            action=Action.PASSWORD_RESET, admin=principal(maker), target=customer,
        )
        _, revealed = admin_services.approve(change.id, admin=principal(checker))
        temporary = revealed["temporary_password"]
        services.login(email=customer.email, password=temporary)

        services.change_password(
            customer.id, current_password=temporary, new_password="a-much-better-one",
        )

        customer.refresh_from_db()
        assert customer.must_change_password is False
        assert admin_services.active_sessions(customer) == 0
        assert services.login(email=customer.email, password="a-much-better-one")

    def test_the_current_password_is_always_required(self, customer):
        """Even under a forced reset. The temporary credential was handed over
        out of band, and proving you hold it is the only thing separating the
        intended recipient from whoever saw it in transit."""
        from identity.services import AuthenticationFailed

        with pytest.raises(AuthenticationFailed):
            services.change_password(
                customer.id, current_password="wrong", new_password="a-much-better-one",
            )


class TestRoleChangeInvalidatesSessions:
    def test_a_demotion_kills_existing_tokens(self, maker, checker):
        """The old access token carries the old role for up to 15 minutes. The
        refresh families have to die or the demotion takes an hour to bite."""
        analyst = make_user("analyst@indbank.test", Role.FRAUD_ANALYST)
        services.login(email=analyst.email, password="correct-horse-battery")
        assert admin_services.active_sessions(analyst) == 1

        change = admin_services.request_change(
            action=Action.ROLE_CHANGE, admin=principal(maker), target=analyst,
            payload={"role": Role.CUSTOMER},
        )
        admin_services.approve(change.id, admin=principal(checker))

        assert admin_services.active_sessions(analyst) == 0
        assert RefreshToken.objects.filter(
            user=analyst, revoked_at__isnull=True
        ).count() == 0


class TestValidationAtRequestTime:
    """Queueing a change that can never be approved wastes the approver's
    attention, which is the scarce resource the whole control depends on."""

    def test_a_no_op_role_change_is_refused(self, maker, customer):
        with pytest.raises(ValidationFailed):
            admin_services.request_change(
                action=Action.ROLE_CHANGE, admin=principal(maker), target=customer,
                payload={"role": Role.CUSTOMER},
            )

    def test_an_unknown_role_is_refused(self, maker, customer):
        with pytest.raises(ValidationFailed):
            admin_services.request_change(
                action=Action.ROLE_CHANGE, admin=principal(maker), target=customer,
                payload={"role": "SUPERUSER"},
            )

    def test_unlocking_an_unlocked_account_is_refused(self, maker, customer):
        with pytest.raises(ValidationFailed):
            admin_services.request_change(
                action=Action.UNLOCK, admin=principal(maker), target=customer,
            )

    def test_an_immediate_action_cannot_be_smuggled_through_the_queue(self, maker, customer):
        with pytest.raises(ValidationFailed):
            admin_services.request_change(
                action="LOCK", admin=principal(maker), target=customer,
            )

    def test_a_missing_target_is_refused(self, maker):
        with pytest.raises(ValidationFailed):
            admin_services.request_change(
                action=Action.CLOSE, admin=principal(maker), target=None,
            )


class TestDirectory:
    def test_search_matches_email_and_name(self, maker, customer):
        rows, total = admin_services.list_users(q="asha")
        assert total == 1
        assert rows[0].email == customer.email

    def test_filters_combine(self, maker, checker, customer):
        rows, total = admin_services.list_users(role=Role.ADMIN)
        assert total == 2
        assert {u.email for u in rows} == {maker.email, checker.email}

    def test_stats_count_the_queue(self, maker, checker, customer):
        admin_services.request_change(
            action=Action.ROLE_CHANGE, admin=principal(maker), target=customer,
            payload={"role": Role.OPS},
        )
        stats = admin_services.stats()

        assert stats["by_role"][Role.ADMIN] == 2
        assert stats["pending_approvals"] == 1

    def test_a_missing_change_request_is_a_404(self, checker):
        with pytest.raises(NotFound):
            admin_services.approve(uuid.uuid4(), admin=principal(checker))
