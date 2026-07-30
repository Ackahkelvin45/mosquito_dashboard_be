"""Providers (webpush mocked, registry selection, stubs) and channels
(push fan-out with failure accounting, email, delivery bookkeeping)."""
from types import SimpleNamespace

import pytest
from pywebpush import WebPushException

import app.notification.channels.email as email_module
import app.notification.channels.push as push_module
import app.notification.providers.registry as registry_module
import app.notification.providers.webpush as webpush_module
from app.notification.channels.email import EmailChannel
from app.notification.channels.push import PushChannel, build_push_payload
from app.notification.enums import (
    DeliveryChannel,
    DeliveryStatus,
    PushProvider,
)
from app.notification.models import NotificationDelivery, PushSubscription
from app.notification.providers.apns import APNSProvider
from app.notification.providers.base import PushResult
from app.notification.providers.fcm import FCMProvider
from app.notification.providers.registry import get_provider
from app.notification.providers.webpush import WebPushProvider, is_webpush_configured


@pytest.fixture
def vapid_configured(monkeypatch):
    monkeypatch.setattr(webpush_module, "VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setattr(webpush_module, "VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(webpush_module, "VAPID_CLAIMS_EMAIL", "ops@example.com")


@pytest.fixture
def vapid_unconfigured(monkeypatch):
    monkeypatch.setattr(webpush_module, "VAPID_PUBLIC_KEY", None)
    monkeypatch.setattr(webpush_module, "VAPID_PRIVATE_KEY", None)
    monkeypatch.setattr(webpush_module, "VAPID_CLAIMS_EMAIL", None)


@pytest.fixture
def fresh_registry(monkeypatch):
    monkeypatch.setattr(registry_module, "_webpush_instance", None)
    monkeypatch.setattr(registry_module, "_logged_unavailable", set())


@pytest.fixture
def subscription(db_session, make_user):
    user = make_user()
    row = PushSubscription(
        user_id=user.id, endpoint="https://push.example.com/sub",
        p256dh="p", auth="a",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    row._user = user  # convenience for tests
    return row


# ── WebPush provider ─────────────────────────────────────────────────────────

class TestWebPushProvider:
    def test_success(self, monkeypatch, vapid_configured, subscription):
        sent = {}

        def fake_webpush(subscription_info, data, vapid_private_key, vapid_claims):
            sent.update(info=subscription_info, data=data,
                        key=vapid_private_key, claims=vapid_claims)

        monkeypatch.setattr(webpush_module, "webpush", fake_webpush)
        result = WebPushProvider().send(subscription, {"title": "hi"})
        assert result == PushResult(success=True)
        assert sent["info"]["endpoint"] == subscription.endpoint
        assert sent["info"]["keys"] == {"p256dh": "p", "auth": "a"}
        assert sent["key"] == "priv"
        assert sent["claims"] == {"sub": "mailto:ops@example.com"}

    def test_generic_failure(self, monkeypatch, vapid_configured, subscription):
        def fake_webpush(**_kwargs):
            raise RuntimeError("network down")

        monkeypatch.setattr(webpush_module, "webpush", fake_webpush)
        result = WebPushProvider().send(subscription, {})
        assert result.success is False
        assert result.should_deactivate is False
        assert "network down" in result.error

    @pytest.mark.parametrize("status_code", [404, 410])
    def test_gone_subscription_flags_deactivate(self, monkeypatch, vapid_configured,
                                                subscription, status_code):
        def fake_webpush(**_kwargs):
            raise WebPushException(
                "gone", response=SimpleNamespace(status_code=status_code)
            )

        monkeypatch.setattr(webpush_module, "webpush", fake_webpush)
        result = WebPushProvider().send(subscription, {})
        assert result.success is False
        assert result.should_deactivate is True

    def test_webpush_exception_other_status_no_deactivate(self, monkeypatch,
                                                          vapid_configured,
                                                          subscription):
        def fake_webpush(**_kwargs):
            raise WebPushException(
                "throttled", response=SimpleNamespace(status_code=429)
            )

        monkeypatch.setattr(webpush_module, "webpush", fake_webpush)
        result = WebPushProvider().send(subscription, {})
        assert result.success is False
        assert result.should_deactivate is False

    def test_pywebpush_missing(self, monkeypatch, subscription):
        monkeypatch.setattr(webpush_module, "webpush", None)
        result = WebPushProvider().send(subscription, {})
        assert result.success is False
        assert "not installed" in result.error

    def test_is_configured(self, vapid_configured):
        assert is_webpush_configured() is True

    def test_is_not_configured(self, vapid_unconfigured):
        assert is_webpush_configured() is False


# ── Registry ─────────────────────────────────────────────────────────────────

class TestRegistry:
    def test_webpush_none_when_unconfigured(self, vapid_unconfigured, fresh_registry):
        assert get_provider(PushProvider.WEBPUSH) is None

    def test_webpush_instance_when_configured_and_cached(self, vapid_configured,
                                                         fresh_registry):
        provider = get_provider(PushProvider.WEBPUSH)
        assert isinstance(provider, WebPushProvider)
        assert get_provider(PushProvider.WEBPUSH) is provider  # cached singleton

    @pytest.mark.parametrize("provider", [PushProvider.FCM, PushProvider.APNS])
    def test_stub_providers_resolve_to_none(self, provider, fresh_registry):
        assert get_provider(provider) is None

    @pytest.mark.parametrize("stub_class", [FCMProvider, APNSProvider])
    def test_stub_classes_raise(self, stub_class, subscription):
        with pytest.raises(NotImplementedError):
            stub_class().send(subscription, {})


# ── Push channel ─────────────────────────────────────────────────────────────

class FakeProvider:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def send(self, subscription, payload):
        self.calls.append((subscription.id, payload))
        return self.results.pop(0)


class TestPushChannel:
    def _notification(self, make_notification, user):
        return make_notification(user)

    def test_payload_shape(self, make_user, make_notification):
        user = make_user()
        n = make_notification(user, payload={"voltage": 3.1}, icon="battery-low",
                              action_url="/devices/1")
        payload = build_push_payload(n)
        assert payload == {
            "notification_id": n.id,
            "title": n.title,
            "body": n.body,
            "icon": "battery-low",
            "action_url": "/devices/1",
            "severity": "INFO",
            "category": "SYSTEM",
            "type": "TEST",
            "payload": {"voltage": 3.1},
        }

    def test_success_records_and_resets_failures(self, db_session, monkeypatch,
                                                 subscription, make_notification):
        subscription.failure_count = 3
        db_session.commit()
        provider = FakeProvider([PushResult(success=True)])
        monkeypatch.setattr(push_module, "get_provider", lambda p: provider)
        n = make_notification(subscription._user)
        assert PushChannel(db_session).send(n, subscription._user) is True
        db_session.refresh(subscription)
        assert subscription.failure_count == 0
        assert subscription.last_used_at is not None
        assert subscription.active is True

    def test_failure_increments_failure_count(self, db_session, monkeypatch,
                                              subscription, make_notification):
        provider = FakeProvider([PushResult(success=False, error="x")])
        monkeypatch.setattr(push_module, "get_provider", lambda p: provider)
        n = make_notification(subscription._user)
        assert PushChannel(db_session).send(n, subscription._user) is False
        db_session.refresh(subscription)
        assert subscription.failure_count == 1
        assert subscription.active is True

    def test_deactivates_after_five_consecutive_failures(self, db_session, monkeypatch,
                                                         subscription,
                                                         make_notification):
        provider = FakeProvider([PushResult(success=False, error="x")] * 5)
        monkeypatch.setattr(push_module, "get_provider", lambda p: provider)
        n = make_notification(subscription._user)
        channel = PushChannel(db_session)
        for _ in range(5):
            channel.send(n, subscription._user)
            db_session.refresh(subscription)
            if not subscription.active:
                break
        assert subscription.failure_count == 5
        assert subscription.active is False
        # Deactivated -> no longer targeted at all.
        assert channel.send(n, subscription._user) is False
        assert len(provider.calls) == 5

    def test_should_deactivate_kills_subscription_immediately(self, db_session,
                                                              monkeypatch,
                                                              subscription,
                                                              make_notification):
        provider = FakeProvider([PushResult(success=False, should_deactivate=True,
                                            error="410 gone")])
        monkeypatch.setattr(push_module, "get_provider", lambda p: provider)
        n = make_notification(subscription._user)
        assert PushChannel(db_session).send(n, subscription._user) is False
        db_session.refresh(subscription)
        assert subscription.active is False
        assert subscription.failure_count == 0  # deactivated, not failure-counted

    def test_mixed_subscriptions_any_success_wins(self, db_session, monkeypatch,
                                                  make_user, make_notification):
        user = make_user()
        for index in range(2):
            db_session.add(PushSubscription(
                user_id=user.id, endpoint=f"https://push.example.com/m{index}",
                p256dh="p", auth="a",
            ))
        db_session.commit()
        provider = FakeProvider([PushResult(success=False, error="x"),
                                 PushResult(success=True)])
        monkeypatch.setattr(push_module, "get_provider", lambda p: provider)
        n = make_notification(user)
        assert PushChannel(db_session).send(n, user) is True
        assert len(provider.calls) == 2

    def test_no_subscriptions_returns_false(self, db_session, make_user,
                                            make_notification):
        user = make_user()
        n = make_notification(user)
        assert PushChannel(db_session).send(n, user) is False

    def test_unconfigured_provider_skipped(self, db_session, monkeypatch,
                                           subscription, make_notification):
        monkeypatch.setattr(push_module, "get_provider", lambda p: None)
        n = make_notification(subscription._user)
        assert PushChannel(db_session).send(n, subscription._user) is False
        db_session.refresh(subscription)
        assert subscription.failure_count == 0  # not counted as a failure

    def test_provider_exception_never_raises(self, db_session, monkeypatch,
                                             subscription, make_notification):
        class ExplodingProvider:
            def send(self, subscription, payload):
                raise RuntimeError("boom")

        monkeypatch.setattr(push_module, "get_provider",
                            lambda p: ExplodingProvider())
        n = make_notification(subscription._user)
        assert PushChannel(db_session).send(n, subscription._user) is False


# ── Service delivery bookkeeping (send_push / send_email) ────────────────────

class TestServiceDelivery:
    def test_send_push_records_sent_delivery(self, db_session, monkeypatch, service,
                                             subscription, make_notification):
        provider = FakeProvider([PushResult(success=True)])
        monkeypatch.setattr(push_module, "get_provider", lambda p: provider)
        n = make_notification(subscription._user)
        notification_row = service.notification_repository.get_owned(
            n.id, subscription._user.id
        )
        assert service.send_push(notification_row, subscription._user.id) is True
        delivery = db_session.query(NotificationDelivery).one()
        assert delivery.channel == DeliveryChannel.PUSH
        assert delivery.status == DeliveryStatus.SENT
        assert delivery.attempts == 1
        assert delivery.error is None

    def test_send_push_records_failed_delivery(self, db_session, monkeypatch, service,
                                               subscription, make_notification):
        provider = FakeProvider([PushResult(success=False, error="x")])
        monkeypatch.setattr(push_module, "get_provider", lambda p: provider)
        n = make_notification(subscription._user)
        notification_row = service.notification_repository.get_owned(
            n.id, subscription._user.id
        )
        assert service.send_push(notification_row, subscription._user.id) is False
        delivery = db_session.query(NotificationDelivery).one()
        assert delivery.status == DeliveryStatus.FAILED
        assert delivery.error == "all push attempts failed"

    def test_send_push_no_subscriptions_no_delivery_row(self, db_session, service,
                                                        make_user, make_notification):
        user = make_user()
        n = make_notification(user)
        row = service.notification_repository.get_owned(n.id, user.id)
        assert service.send_push(row, user.id) is False
        assert db_session.query(NotificationDelivery).count() == 0

    def test_send_push_unknown_user(self, db_session, service, make_user,
                                    make_notification):
        user = make_user()
        n = make_notification(user)
        row = service.notification_repository.get_owned(n.id, user.id)
        assert service.send_push(row, 999999) is False

    def test_send_email_records_delivery(self, db_session, monkeypatch, service,
                                         make_user, make_notification):
        sent = []
        monkeypatch.setattr(email_module, "send_email",
                            lambda to, subject, body: sent.append((to, subject)))
        user = make_user()
        n = make_notification(user)
        row = service.notification_repository.get_owned(n.id, user.id)
        assert service.send_email(row, user.id) is True
        assert sent == [(user.email, n.title)]
        delivery = db_session.query(NotificationDelivery).one()
        assert delivery.channel == DeliveryChannel.EMAIL
        assert delivery.status == DeliveryStatus.SENT

    def test_send_email_failure_recorded(self, db_session, monkeypatch, service,
                                         make_user, make_notification):
        def explode(**_kwargs):
            raise RuntimeError("smtp down")

        monkeypatch.setattr(email_module, "send_email", explode)
        user = make_user()
        n = make_notification(user)
        row = service.notification_repository.get_owned(n.id, user.id)
        assert service.send_email(row, user.id) is False
        delivery = db_session.query(NotificationDelivery).one()
        assert delivery.status == DeliveryStatus.FAILED
        assert delivery.error == "email send failed"


# ── Email channel HTML ───────────────────────────────────────────────────────

class TestEmailChannel:
    def test_html_escapes_and_links(self, monkeypatch, db_session, make_user,
                                    make_notification):
        captured = {}
        monkeypatch.setattr(
            email_module, "send_email",
            lambda to, subject, body: captured.update(to=to, subject=subject,
                                                      body=body),
        )
        user = make_user(first_name="Ama")
        n = make_notification(user, title="<b>Alert</b>", body="a & b",
                              action_url="/devices/7")
        assert EmailChannel(db_session).send(n, user) is True
        assert captured["subject"] == "<b>Alert</b>"
        assert "&lt;b&gt;Alert&lt;/b&gt;" in captured["body"]  # escaped
        assert "a &amp; b" in captured["body"]
        assert "Hi Ama," in captured["body"]
        assert f"{email_module.DASHBOARD_BASE_URL}/devices/7" in captured["body"]

    def test_absolute_action_url_used_verbatim(self, monkeypatch, db_session,
                                               make_user, make_notification):
        captured = {}
        monkeypatch.setattr(
            email_module, "send_email",
            lambda to, subject, body: captured.update(body=body),
        )
        user = make_user()
        n = make_notification(user, action_url="https://elsewhere.example.com/x")
        EmailChannel(db_session).send(n, user)
        assert "https://elsewhere.example.com/x" in captured["body"]

    def test_no_action_url_no_button(self, monkeypatch, db_session, make_user,
                                     make_notification):
        captured = {}
        monkeypatch.setattr(
            email_module, "send_email",
            lambda to, subject, body: captured.update(body=body),
        )
        user = make_user()
        n = make_notification(user, action_url=None)
        EmailChannel(db_session).send(n, user)
        assert "View in Dashboard" not in captured["body"]


# ── Background dispatch plumbing ─────────────────────────────────────────────

class TestDeliverChannels:
    def test_deliver_channels_invokes_both_channels(self, monkeypatch,
                                                    TestingSessionLocal, make_user,
                                                    make_notification):
        import app.notification.service as service_module

        monkeypatch.setattr(service_module, "SessionLocal", TestingSessionLocal)
        calls = []
        monkeypatch.setattr(
            service_module.NotificationService, "send_push",
            lambda self, notification, user_id: calls.append(("push", notification.id,
                                                              user_id)),
        )
        monkeypatch.setattr(
            service_module.NotificationService, "send_email",
            lambda self, notification, user_id: calls.append(("email", notification.id,
                                                              user_id)),
        )
        user = make_user()
        n = make_notification(user)
        service_module._deliver_channels([
            (n.id, user.id, True, True),
            (999999, user.id, True, False),  # vanished notification -> skipped
        ])
        assert calls == [("push", n.id, user.id), ("email", n.id, user.id)]

    def test_deliver_channels_swallows_channel_errors(self, monkeypatch,
                                                      TestingSessionLocal, make_user,
                                                      make_notification):
        import app.notification.service as service_module

        monkeypatch.setattr(service_module, "SessionLocal", TestingSessionLocal)

        def explode(self, notification, user_id):
            raise RuntimeError("boom")

        monkeypatch.setattr(service_module.NotificationService, "send_push", explode)
        user = make_user()
        n = make_notification(user)
        # Must not raise.
        service_module._deliver_channels([(n.id, user.id, True, False)])

    def test_dispatch_submits_to_worker_pool_in_production(self, monkeypatch):
        """The real _dispatch_channels (recorder-free original, captured by
        conftest before patching) submits _deliver_channels to the bounded
        dispatch executor — off the calling thread."""
        import app.notification.service as service_module
        from tests.conftest import ORIGINAL_DISPATCH_CHANNELS

        submitted = {}

        class FakeExecutor:
            def submit(self, fn, *args):
                submitted.update(fn=fn, args=args)

        monkeypatch.setattr(service_module, "_DISPATCH_EXECUTOR", FakeExecutor())
        entries = [(1, 2, True, False)]
        ORIGINAL_DISPATCH_CHANNELS(entries)
        assert submitted["fn"] is service_module._deliver_channels
        assert submitted["args"] == (entries,)

    def test_dispatch_swallows_executor_shutdown(self, monkeypatch):
        import app.notification.service as service_module
        from tests.conftest import ORIGINAL_DISPATCH_CHANNELS

        class DeadExecutor:
            def submit(self, fn, *args):
                raise RuntimeError("cannot schedule new futures after shutdown")

        monkeypatch.setattr(service_module, "_DISPATCH_EXECUTOR", DeadExecutor())
        # Must not raise — dropped with a warning.
        ORIGINAL_DISPATCH_CHANNELS([(1, 2, True, False)])
