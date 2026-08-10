"""Trap-status normalisation.

Per MQTT_Schema_Reference.pdf, real device firmware reports trap_status as
the string "ON" or "OFF" — never a boolean. Shared by both ingestion paths
(raw MQTT dict and the REST Pydantic payload) so a stray/legacy value never
silently miscoerces: a non-empty string like "OFF" is truthy in Python, so
without this a naive `bool("OFF")` would store the trap as ON.
"""
import logging

logger = logging.getLogger(__name__)


def parse_trap_status(value, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        upper = value.strip().upper()
        if upper == "ON":
            return True
        if upper == "OFF":
            return False
        logger.warning("Unexpected trap_status value %r, defaulting to %s", value, default)
        return default
    return bool(value)


def demo():
    assert parse_trap_status("ON") is True
    assert parse_trap_status("off") is False
    assert parse_trap_status(True) is True
    assert parse_trap_status(False) is False
    assert parse_trap_status(None) is False
    assert parse_trap_status(None, default=True) is True
    assert parse_trap_status("garbage", default=True) is True
    print("ok")


if __name__ == "__main__":
    demo()
