from __future__ import annotations

import signal
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable


@dataclass
class ScheduledJob:
    name: str
    interval_seconds: int
    run: Callable[[], None]
    next_run: datetime


class DataOpsScheduler:
    def __init__(self, jobs: list[ScheduledJob], sleep_seconds: int = 30):
        self.jobs = jobs
        self.sleep_seconds = max(1, sleep_seconds)
        self._running = True

    def stop(self, *_args) -> None:
        self._running = False

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while self._running:
            now = datetime.now(timezone.utc)
            for job in self.jobs:
                if now >= job.next_run:
                    job.run()
                    job.next_run = now + timedelta(seconds=job.interval_seconds)
            time.sleep(self.sleep_seconds)


def scheduled_job(name: str, interval_seconds: int, run: Callable[[], None]) -> ScheduledJob:
    return ScheduledJob(
        name=name,
        interval_seconds=interval_seconds,
        run=run,
        next_run=datetime.now(timezone.utc),
    )
