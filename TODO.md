# TODO

## Unregistered-device detection (planned, not yet built)

**Problem (verified 2026-07-29):** MQTT data published under a `device_uuid`
that isn't registered is **silently dropped** — logged to the server console
only, invisible in the dashboard. If a trap is deployed before (or mistyped
during) registration, its data vanishes with no signal to anyone.

**Goal:** a mismatch should be impossible to miss — turn the silent failure
into a visible prompt with one-click recovery.

### Plan

1. **Track unknown publishers** — in `app/core/mqtt_client.py`, when
   `on_message` fails to find the device, record the UUID instead of only
   logging:
   - table `unregistered_device_sightings`:
     `device_uuid (unique)`, `first_seen`, `last_seen`, `message_count`,
     `last_topic`, `last_payload (JSONB, truncated)` — persistent, so sightings
     survive restarts; upsert per message (cheap, one row per UUID).
   - keep the last payload so registration can prefill location if the payload
     carried a GPS fix.
2. **API** (auth required):
   - `GET /devices/unregistered` → list of sightings (uuid, first/last seen,
     message count).
   - `DELETE /devices/unregistered/{uuid}` → dismiss/ignore a stray UUID
     (e.g. a neighbour's test device).
   - On successful `POST /devices` with a matching uuid, delete its sighting
     automatically.
3. **Frontend:**
   - Dashboard banner: "⚠ 2 devices are sending data but aren't registered"
     → links to the devices page.
   - Devices page section listing sightings, each with a **Register** button
     that opens the add-device form with `device_uuid` prefilled (and
     lat/long from the last payload when present).
4. **Verify:** publish to an unregistered uuid → sighting appears via API →
   register from it → sighting cleared → data lands. Regression: existing
   MQTT flow suite (`verify_mqtt_flow.py` scenario) still 13/13.

### Notes
- Ingest hot path must stay cheap: the upsert is one indexed write; consider
  throttling upserts per uuid (e.g. update `last_seen` at most once/min).
- Don't auto-register devices — an unknown publisher may be noise/abuse;
  a human confirms via the banner flow.
- Alembic migration needed for the new table (head is `e5f6a7b8c9d0`).
