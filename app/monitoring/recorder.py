"""Ingest instrumentation used by the MQTT client (and broker lifecycle hooks).

Every function here MUST be failure-proof: observability must never cost a
reading. Anything that throws is logged and swallowed, mirroring emit()'s
contract in app.notification.events.

Broker state is in-memory on purpose — the API runs as a single process (see
Dockerfile), so the one MQTT client's view IS the truth. It resets on restart;
`connected=None` therefore means "not known yet", distinct from False.
"""
import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from app.monitoring.models import MqttIngestError, MqttTrafficHourly

logger = logging.getLogger(__name__)

# ── In-memory broker/pipeline state ──────────────────────────────────────────

broker_state: dict = {
    "connected": None,        # None = unknown (e.g. just restarted)
    "since": None,            # when the current connected/disconnected state began
    "last_message_at": None,  # last MQTT message received (any device, any topic)
    "started_at": datetime.utcnow(),
}


def mark_connected() -> None:
    broker_state["connected"] = True
    broker_state["since"] = datetime.utcnow()


def mark_disconnected() -> None:
    broker_state["connected"] = False
    broker_state["since"] = datetime.utcnow()


def mark_message() -> None:
    broker_state["last_message_at"] = datetime.utcnow()


# ── DB recorders ─────────────────────────────────────────────────────────────

def _hour_bucket(now: datetime | None = None) -> datetime:
    now = now or datetime.utcnow()
    return now.replace(minute=0, second=0, microsecond=0)


def record_message(db, device_id: int, event_type: str, is_test: bool) -> None:
    """Count one successfully-stored message into its hourly bucket.

    Messages are handled sequentially by the single MQTT client, so a plain
    select-then-update is race-free in practice; the IntegrityError branch
    covers the REST ingest path landing concurrently on a fresh bucket.
    """
    try:
        bucket = _hour_bucket()
        row = (
            db.query(MqttTrafficHourly)
            .filter(
                MqttTrafficHourly.bucket_start == bucket,
                MqttTrafficHourly.device_id == device_id,
                MqttTrafficHourly.event_type == event_type,
                MqttTrafficHourly.is_test.is_(is_test),
            )
            .first()
        )
        if row:
            row.message_count += 1
        else:
            db.add(MqttTrafficHourly(
                bucket_start=bucket, device_id=device_id,
                event_type=event_type, is_test=is_test, message_count=1,
            ))
        db.commit()
    except IntegrityError:
        db.rollback()
        try:
            db.query(MqttTrafficHourly).filter(
                MqttTrafficHourly.bucket_start == bucket,
                MqttTrafficHourly.device_id == device_id,
                MqttTrafficHourly.event_type == event_type,
                MqttTrafficHourly.is_test.is_(is_test),
            ).update({"message_count": MqttTrafficHourly.message_count + 1})
            db.commit()
        except Exception:
            logger.exception("Traffic counter retry failed")
            db.rollback()
    except Exception:
        logger.exception("Could not record MQTT traffic")
        try:
            db.rollback()
        except Exception:
            pass


def record_error(db, error_type: str, *, topic: str | None = None,
                 device_uuid: str | None = None, detail: str | None = None) -> None:
    """Store one raw ingest failure for the System Health error feed."""
    try:
        db.add(MqttIngestError(
            error_type=error_type,
            topic=(topic or None) and str(topic)[:255],
            device_uuid=(device_uuid or None) and str(device_uuid)[:100],
            detail=(detail or None) and str(detail)[:500],
        ))
        db.commit()
    except Exception:
        logger.exception("Could not record MQTT ingest error")
        try:
            db.rollback()
        except Exception:
            pass
