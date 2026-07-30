"""Dependency-free asyncio scheduler for the notification background jobs.

One asyncio.Task per job, started from the FastAPI lifespan after MQTT is up.
Each job is a plain sync callable that opens its own SessionLocal and is run
via asyncio.to_thread, so the event loop is never blocked. Every execution is
wrapped in a blanket except — a job failure can never kill its loop or the
app. NOTIFY_JOBS_ENABLED=0 disables the whole subsystem.

Supports fixed intervals, daily-at-HH:MM (UTC) and weekly-at (weekday+time,
UTC). All time math is naive UTC via datetime.utcnow (house convention).
"""
import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Seconds of stagger between job start-ups so they don't all hit the DB at once.
STARTUP_JITTER_SECONDS = 5.0
# Pause before retrying when the loop itself errors (bad schedule config, ...).
LOOP_ERROR_PAUSE_SECONDS = 30.0


def _env_bool(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", ""}


@dataclass
class Job:
    """A scheduled job: exactly one of interval_seconds / daily_at / weekly_at."""
    name: str
    func: Callable[[], None]  # sync; opens its own SessionLocal inside
    interval_seconds: Optional[float] = None
    daily_at: Optional[time] = None                 # UTC
    weekly_at: Optional[tuple[int, time]] = None    # (weekday, time) UTC; 0=Monday

    def seconds_until_next_run(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.utcnow()
        if self.interval_seconds is not None:
            return float(self.interval_seconds)
        if self.daily_at is not None:
            target = datetime.combine(now.date(), self.daily_at)
            if target <= now:
                target += timedelta(days=1)
            return (target - now).total_seconds()
        if self.weekly_at is not None:
            weekday, at = self.weekly_at
            target = datetime.combine(
                now.date() + timedelta(days=(weekday - now.weekday()) % 7), at
            )
            if target <= now:
                target += timedelta(days=7)
            return (target - now).total_seconds()
        raise ValueError(f"Job {self.name!r} has no schedule configured")


class Scheduler:
    def __init__(self) -> None:
        self._jobs: list[Job] = []
        self._tasks: list[asyncio.Task] = []

    @property
    def jobs(self) -> list[Job]:
        return list(self._jobs)

    def register(self, job: Job) -> None:
        self._jobs.append(job)

    async def start(self) -> None:
        if not _env_bool("NOTIFY_JOBS_ENABLED", "1"):
            logger.info("Background jobs are disabled (NOTIFY_JOBS_ENABLED=0)")
            return
        if self._tasks:
            return  # already running
        for index, job in enumerate(self._jobs):
            self._tasks.append(
                asyncio.create_task(
                    self._run_loop(job, index * STARTUP_JITTER_SECONDS),
                    name=f"job-{job.name}",
                )
            )
        logger.info(
            "Scheduler started %d job(s): %s",
            len(self._jobs), ", ".join(job.name for job in self._jobs),
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        logger.info("Scheduler stopped")

    async def _run_loop(self, job: Job, initial_delay: float) -> None:
        if initial_delay:
            await asyncio.sleep(initial_delay)
        while True:
            try:
                await asyncio.sleep(job.seconds_until_next_run())
                await asyncio.to_thread(self._execute, job)
            except asyncio.CancelledError:
                raise
            except Exception:  # scheduling error — never kill the loop
                logger.exception("Job %s loop error", job.name)
                await asyncio.sleep(LOOP_ERROR_PAUSE_SECONDS)

    @staticmethod
    def _execute(job: Job) -> None:
        started = datetime.utcnow()
        try:
            job.func()
        except Exception:  # a job failure must never propagate
            logger.exception("Job %s failed", job.name)
        else:
            logger.debug(
                "Job %s finished in %.1fs",
                job.name, (datetime.utcnow() - started).total_seconds(),
            )


scheduler = Scheduler()


def _parse_utc_time(value: str, fallback: time) -> time:
    try:
        hour, minute = value.strip().split(":")
        return time(int(hour), int(minute))
    except Exception:
        logger.warning("Invalid NOTIFY_DAILY_SUMMARY_UTC %r — using %s", value, fallback)
        return fallback


def register_all_jobs() -> None:
    """Wire every notification job with its env-tunable schedule. Idempotent."""
    if scheduler.jobs:
        return
    # Imported lazily so this module stays stdlib-only (importable standalone).
    from app.jobs.cleanup import run_notification_cleanup
    from app.jobs.health import run_health_sweep
    from app.jobs.offline_detection import run_offline_detection
    from app.jobs.push_retry import run_push_retry
    from app.jobs.summaries import run_daily_summary, run_weekly_summary

    summary_at = _parse_utc_time(
        os.getenv("NOTIFY_DAILY_SUMMARY_UTC", "07:00"), time(7, 0)
    )
    scheduler.register(Job(
        "offline-detection", run_offline_detection,
        interval_seconds=float(os.getenv("NOTIFY_OFFLINE_CHECK_SEC", "120")),
    ))
    scheduler.register(Job(
        "notification-cleanup", run_notification_cleanup,
        interval_seconds=float(os.getenv("NOTIFY_CLEANUP_SEC", "3600")),
    ))
    scheduler.register(Job(
        "push-retry", run_push_retry,
        interval_seconds=float(os.getenv("NOTIFY_PUSH_RETRY_SEC", "600")),
    ))
    scheduler.register(Job("daily-summary", run_daily_summary, daily_at=summary_at))
    scheduler.register(Job("weekly-summary", run_weekly_summary, weekly_at=(0, summary_at)))
    scheduler.register(Job(
        "device-health", run_health_sweep,
        interval_seconds=float(os.getenv("NOTIFY_HEALTH_CHECK_SEC", "86400")),
    ))
