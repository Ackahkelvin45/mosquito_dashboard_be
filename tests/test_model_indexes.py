"""Guard the model-declared indexes that the migrations also create."""
import pytest

from app.authentication import models as _auth_models  # noqa: F401
from app.core.database import Base
from app.device import models as _device_models  # noqa: F401

EXPECTED = [
    ("mosquito_events", "ix_mosquito_events_device_timestamp", ["device_id", "timestamp"]),
    ("mosquito_events", "ix_mosquito_events_timestamp_id", ["timestamp", "id"]),
    ("sensor_device_readings", "ix_sensor_readings_device_timestamp", ["device_id", "timestamp"]),
    ("sensor_device_readings", "ix_sensor_device_readings_timestamp_id", ["timestamp", "id"]),
    ("users", "ix_users_cluster_id", ["cluster_id"]),
]


@pytest.mark.parametrize("table,name,columns", EXPECTED)
def test_index_declared_on_model(table, name, columns):
    indexes = {ix.name: [c.name for c in ix.columns] for ix in Base.metadata.tables[table].indexes}
    assert indexes.get(name) == columns, f"{name} missing or wrong on {table}: {indexes}"
