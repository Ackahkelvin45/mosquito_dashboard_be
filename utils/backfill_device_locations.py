"""One-off: normalise stored device coordinates and fill region/community.

Existing rows predate normalisation-on-ingest, so some hold scaled integers
(device 5 stored longitude -168572 = -0.168572 in microdegrees) and some carry
a hand-typed region that does not match their coordinates.

    uv run python -m utils.backfill_device_locations           # dry run
    uv run python -m utils.backfill_device_locations --apply   # write

Only touches rows whose value actually changes.
"""
import argparse
import logging

from app.core.database import SessionLocal
from app.device.models import Device
from app.authentication.models import User, ResearcherRequest  # noqa: F401  (mapper registry)
from app.service.geocoding_service import reverse_geocode
from utils.coordinates import normalize_lat_lon

logging.basicConfig(level=logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--skip-geocode", action="store_true", help="only normalise coordinates")
    args = parser.parse_args()

    with SessionLocal() as session:
        devices = session.query(Device).order_by(Device.id).all()
        changed = 0

        for device in devices:
            updates = []

            lat, lon = normalize_lat_lon(device.latitude, device.longitude)
            if lat is None or lon is None:
                print(f"device {device.id} ({device.device_uuid}): no usable coordinates — skipped")
                continue

            if lat != device.latitude or lon != device.longitude:
                updates.append(
                    f"coords {device.latitude},{device.longitude} -> {lat},{lon}"
                )
                device.latitude, device.longitude = lat, lon

            if not args.skip_geocode:
                region, community = reverse_geocode(lat, lon)
                if region and region != device.region:
                    updates.append(f"region {device.region!r} -> {region!r}")
                    device.region = region
                if community and community != device.community:
                    updates.append(f"community {device.community!r} -> {community!r}")
                    device.community = community

            if updates:
                changed += 1
                print(f"device {device.id} ({device.device_uuid}):")
                for u in updates:
                    print(f"    {u}")
            else:
                print(f"device {device.id} ({device.device_uuid}): already correct")

        if args.apply and changed:
            session.commit()
            print(f"\napplied changes to {changed} device(s)")
        elif changed:
            session.rollback()
            print(f"\ndry run — {changed} device(s) would change. Re-run with --apply to write.")
        else:
            print("\nnothing to change")


if __name__ == "__main__":
    main()
