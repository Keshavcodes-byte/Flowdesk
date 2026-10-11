"""Dedicated bounded queue worker and persistent schedule dispatcher."""

from __future__ import annotations

import logging
import signal
import threading
import uuid
from apscheduler.executors.pool import ThreadPoolExecutor as APSchedulerThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app.engine.background import BackgroundExecutionService, dispatch_due_schedules, recover_stale_executions, release_scheduler_lease
from app.storage.database import initialize_database

logger = logging.getLogger("app.worker")


def main() -> None:
    initialize_database()
    recover_stale_executions()
    owner_id = str(uuid.uuid4())
    scheduler = BackgroundScheduler(
        timezone="UTC",
        daemon=True,
        executors={"default": APSchedulerThreadPoolExecutor(max_workers=4)},
        job_defaults={"coalesce": True, "misfire_grace_time": 30},
    )
    service = BackgroundExecutionService()
    scheduler.add_job(
        service.process_one, "interval", seconds=1, id="background-queue-poll",
        max_instances=4, replace_existing=True,
    )
    scheduler.add_job(
        lambda: dispatch_due_schedules(owner_id), "interval", seconds=5,
        id="persistent-schedule-dispatch", max_instances=1, replace_existing=True,
    )
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    scheduler.start()
    logger.info("Flowdesk background worker started", extra={"worker_id": service.worker_id})
    try:
        stop.wait()
    finally:
        scheduler.shutdown(wait=True)
        release_scheduler_lease(owner_id)
        logger.info("Flowdesk background worker stopped", extra={"worker_id": service.worker_id})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
