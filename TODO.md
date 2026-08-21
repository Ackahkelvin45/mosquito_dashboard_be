# TODO

## Unregistered-device detection — ✅ BUILT (2026-08-20)

Implemented per the plan below (kept for reference). Delivered:
`unregistered_device_sightings` table (upserted from the MQTT unknown-device
path in `app/core/mqtt_client.py`, GPS fix parsed into columns for prefill,
payload truncated at 2 KB), `GET /devices/unregistered` +
`DELETE /devices/unregistered/{uuid}` (SUPER_ADMIN), auto-clear on device
registration (`DeviceService.create_device`), dashboard banner + devices-page
section with Register (prefilled add form) / Dismiss buttons. Migration
`a3b4c5d6e7f8`. See API_CHANGES.md §14.

<details>
<summary>Original plan (implemented)</summary>

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

</details>


## Minimum-confidence filter for mosquito detections (FR-15) (planned, not yet built)

**Problem (noted 2026-08-21):** every detection carries a real ML confidence
score — `MosquitoIndividualReading.p_mosq` (0–1, "was this a mosquito at
all") plus per-class confidences in `taxon_probs`/`sex_probs` — but the
dashboard never shows or filters on any of it. A researcher looking at the
data cannot separate a 0.51 borderline detection from a 0.98 certain one,
so noisy low-confidence events dilute every chart and export.

**Goal (spec FR-15, "most confident classification" toggle):** let users view
all detections or only those above a chosen confidence.

### Plan

1. **Backend** — a `min_confidence` query param (float 0–1) on
   `GET /mosquito` and the historical-data endpoints, filtering on
   `MosquitoIndividualReading.p_mosq`; expose `p_mosq` in the row responses
   where it is missing today. Same param on the public `/api/v1` endpoints so
   researchers get it too.
2. **Frontend** — a "Min confidence" control on the Historical Data page
   (follow its existing filter + chip pattern) and show the confidence value
   as a column in the detections table; consider a subtle low-confidence
   style (e.g. muted row) instead of hiding by default.
3. **Exports** — the CSV/PDF exports must respect the active confidence
   filter and include the confidence column.

### Notes
- `taxon_probs` / `sex_probs` are winner-only confidences, NOT probability
  distributions (they don't sum to 1 — see MQTT_Schema_Reference.pdf). Never
  sum, average or normalise them across classes.
- Age grouping stays out of scope: `age_group` is a firmware placeholder
  (always empty).
- Rows with `p_mosq` NULL (older data) should be treated as "unknown", shown
  by default and only hidden when a filter is explicitly set.


## Surface `esp1_link_alive` — detection-offline visibility (planned, not yet built)

**Problem (noted 2026-08-20):** each trap is two boards — the Listener Unit
runs the mosquito-detection ML pipeline, the Gateway Unit publishes to MQTT.
`sensor_data.esp1_link_alive` reports whether the Gateway currently has a live
link to the Listener. A device can therefore be fully **online** (telemetry
flowing, `is_active` green) while its **detection system is dead**. During such
a window, an absence of `mosquito_data` events means **"unknown"**, not "no
mosquitoes" (MQTT_Schema_Reference.pdf is explicit about this) — but the
dashboard currently treats every quiet period as a genuine zero.

That's a dangerous false negative for surveillance data: a link outage reads
as "no vector activity in this community".

**Current state:** the flag is stored faithfully on every reading
(`sensor_device_readings.esp1_link_alive`, nullable for older firmware) and
serialized in device responses via `latest_reading` — but nothing consumes it:
no alert, no badge, no chart treatment.

### Plan

1. **Alert (backend)** — new `DETECTION_LINK_DOWN` notification event, emitted
   from `handle_sensor_data` when a **non-test** reading arrives with
   `esp1_link_alive == false`. Edge-trigger it like TRAP_TRIGGERED (previous
   non-test reading had `true`/`null` → now `false`) so a persistent outage
   doesn't re-page every reading; emit a recovery event (or reuse a dedupe
   window) on the flip back to `true`. Severity WARNING, category DEVICE,
   `action_url=/devices/{id}`.
2. **Badge (frontend)** — on the device detail and map sensor pages, next to
   the existing Device (Online/Offline) and Trap (On/Off) indicators, add
   "Detection: OK / Offline / Unknown" driven by
   `latest_reading.esp1_link_alive` (`null` → Unknown, older firmware).
3. **Chart treatment (optional, later)** — shade windows on mosquito-count
   charts where the flag was `false`, so flat zeros during an outage aren't
   read as real absence. Needs a per-bucket "link was down" aggregate from
   `sensor_device_readings` — more invasive, decide separately.

### Notes
- `null` must stay distinct from `false`: null = firmware never reported the
  flag (unknown), false = a real "link down" report. Don't collapse them.
- Test-mode readings must not trigger the alert (same rule as every other
  notification).
- Steps 1–2 are cheap and independent of step 3; ship them first.
