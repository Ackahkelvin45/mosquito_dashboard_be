"""DELETE /devices/uuid/{uuid}/mosquito-events/{id}: an ADMIN may delete only
on devices in their OWN cluster — not public clusters they can merely see."""
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.authentication.enums import UserRole
from app.core.database import get_db
from app.device.models import MosquitoEvent
from utils.protected_route import get_current_user


@pytest.fixture
def setup(db_session, make_cluster, make_device, make_user):
    from app.authentication.schema import UserResponse
    from app.device.routes import router

    app = FastAPI()
    app.include_router(router, prefix="/devices")
    app.dependency_overrides[get_db] = lambda: db_session
    own, public = make_cluster(), make_cluster(public=True)
    devices = {"own": make_device(own), "public": make_device(public)}
    events = {}
    for k, d in devices.items():
        e = MosquitoEvent(device_id=d.id, timestamp=datetime.utcnow(), count=1)
        db_session.add(e)
        db_session.flush()
        events[k] = e.id
    db_session.commit()

    def as_(user):
        app.dependency_overrides[get_current_user] = lambda: UserResponse.model_validate(user)
        return TestClient(app, raise_server_exceptions=False)

    return {"as": as_, "devices": devices, "events": events, "own": own}


def _delete(client, setup, key):
    return client.delete(f"/devices/uuid/{setup['devices'][key].device_uuid}/mosquito-events/{setup['events'][key]}")


def test_admin_cannot_delete_on_public_cluster(setup, make_user, db_session):
    client = setup["as"](make_user(role=UserRole.ADMIN, cluster_id=setup["own"].id))
    assert _delete(client, setup, "public").status_code == 404
    assert db_session.get(MosquitoEvent, setup["events"]["public"]) is not None
    assert _delete(client, setup, "own").status_code == 204


def test_clusterless_admin_deletes_nothing(setup, make_user):
    client = setup["as"](make_user(role=UserRole.ADMIN))
    assert _delete(client, setup, "public").status_code == 404


def test_super_admin_deletes_anywhere(setup, make_user):
    client = setup["as"](make_user(role=UserRole.SUPER_ADMIN))
    assert _delete(client, setup, "public").status_code == 204
