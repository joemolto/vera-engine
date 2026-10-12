"""In-process scheduler for self-hosted Vera (no external cron needed).

Runs the recurring jobs on a timer:
  - the daily monitor pass over every watched account, which also sends
    the daily digest (settings.MONITOR_HOUR_UTC);
  - a daily check that the monitor pass actually ran, HEARTBEAT_DELAY_HOURS
    later (emails settings.OPERATOR_EMAIL if it didn't);
  - webhook delivery, every WEBHOOK_INTERVAL_SECONDS.

Each job has its own daemon thread, so a long monitor pass never delays
webhook delivery and a job never overlaps itself. Run ONE engine process
(one uvicorn worker); every process starts its own scheduler.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.database import get_supabase

logger = logging.getLogger(__name__)

WEBHOOK_INTERVAL_SECONDS = 30
HEARTBEAT_DELAY_HOURS = 3


def seconds_until(hour_utc: int, now: datetime) -> float:
    """Seconds from `now` to the next hour_utc:00 UTC (always > 0)."""
    nxt = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def _daily_monitor() -> None:
    from app.services.watchlist.batch_monitor import run_monitoring_all_orgs

    while True:
        time.sleep(seconds_until(settings.MONITOR_HOUR_UTC, datetime.now(timezone.utc)))
        try:
            run_monitoring_all_orgs(get_supabase(), tier="solo")
        except Exception as e:  # noqa: BLE001
            logger.error("Scheduled monitor pass failed: %s", e)


def _daily_heartbeat() -> None:
    from app.services.heartbeat import check

    while True:
        hour = (settings.MONITOR_HOUR_UTC + HEARTBEAT_DELAY_HOURS) % 24
        time.sleep(seconds_until(hour, datetime.now(timezone.utc)))
        try:
            check(get_supabase())
        except Exception as e:  # noqa: BLE001
            logger.error("Scheduled heartbeat check failed: %s", e)


def _webhook_delivery() -> None:
    from app.services.webhooks import deliver_pending

    while True:
        try:
            deliver_pending(get_supabase())
        except Exception as e:  # noqa: BLE001
            logger.error("Scheduled webhook delivery failed: %s", e)
        time.sleep(WEBHOOK_INTERVAL_SECONDS)


def start() -> None:
    for job in (_daily_monitor, _daily_heartbeat, _webhook_delivery):
        threading.Thread(target=job, name=job.__name__, daemon=True).start()
    logger.info(
        "Scheduler started: monitor pass daily at %02d:00 UTC, webhook delivery every %ds",
        settings.MONITOR_HOUR_UTC, WEBHOOK_INTERVAL_SECONDS,
    )
