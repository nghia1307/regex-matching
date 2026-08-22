"""
Live progress and cooperative cancellation for a running Spark job.

Progress is not faked from a stopwatch. A background thread polls Spark's own
``StatusTracker`` for the active stages and reads ``numCompletedTasks /
numTasks``, then maps that fraction into the percentage band allocated to the
current phase. So the bar in the browser is driven by real task completion
inside the cluster.

The same thread is the cancellation path. Every action runs inside a *job
group* keyed by the job id, and cancelling means calling
``cancelJobGroup(job_id, interruptOnCancel=True)``: Spark interrupts the running
tasks, the action raises in the driver, and the task exits cleanly. That matters
because ``celery revoke`` alone cannot stop work already executing on an
executor.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

logger = logging.getLogger(__name__)

ProgressSink = Callable[[int, str], None]
CancelCheck = Callable[[], bool]


class JobCancelled(RuntimeError):
    """Raised in the driver once a cancellation has taken effect."""


@dataclass
class _Phase:
    name: str
    low: int
    high: int


class ProgressReporter:
    """Polls Spark for task progress and watches for a cancel request."""

    def __init__(
        self,
        spark,
        job_id: str,
        sink: ProgressSink,
        cancel_check: CancelCheck | None = None,
        interval_seconds: float = 1.5,
    ) -> None:
        self._spark = spark
        self._job_id = str(job_id)
        self._sink = sink
        self._cancel_check = cancel_check or (lambda: False)
        self._interval = interval_seconds
        self._phase = _Phase("starting", 0, 0)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cancel_requested = False
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------- #
    def __enter__(self) -> "ProgressReporter":
        self.start()
        return self

    def __exit__(self, *_exc_info) -> None:
        self.stop()

    def start(self) -> None:
        sc = self._spark.sparkContext
        # Tag every job this thread submits so cancellation can find them.
        sc.setJobGroup(self._job_id, f"regex job {self._job_id}", interruptOnCancel=True)
        self._thread = threading.Thread(
            target=self._loop, name=f"progress-{self._job_id[:8]}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval * 2)
        try:
            self._spark.sparkContext.clearJobGroup()
        except Exception:  # noqa: BLE001 - shutdown path, never mask the real error
            logger.debug("could not clear job group", exc_info=True)

    @property
    def cancelled(self) -> bool:
        return self._cancel_requested

    # -- phases ------------------------------------------------------------- #
    @contextmanager
    def phase(self, name: str, low: int, high: int) -> Iterator["ProgressReporter"]:
        """
        Enter a named phase occupying the percentage band ``[low, high]``.

        Progress inside the band is interpolated from Spark task completion, so a
        long transformation moves smoothly instead of jumping at the end.
        """
        with self._lock:
            self._phase = _Phase(name, low, high)
        self._emit(low, name)
        logger.info("job %s phase=%s", self._job_id, name)
        try:
            yield self
        finally:
            self._emit(high, name)

    def raise_if_cancelled(self) -> None:
        if self._cancel_requested or self._cancel_check():
            raise JobCancelled("job was cancelled")

    # -- internals ---------------------------------------------------------- #
    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - telemetry must never kill a job
                logger.debug("progress tick failed", exc_info=True)

    def _tick(self) -> None:
        if not self._cancel_requested and self._cancel_check():
            self._apply_cancel()
            return

        with self._lock:
            phase = self._phase
        if phase.high <= phase.low:
            return

        fraction = self._task_fraction()
        percent = phase.low + int((phase.high - phase.low) * fraction)
        self._emit(min(percent, phase.high), phase.name)

    def _task_fraction(self) -> float:
        """Completed / total tasks across the currently active stages."""
        try:
            tracker = self._spark.sparkContext.statusTracker()
            stage_ids = tracker.getActiveStageIds()
        except Exception:  # noqa: BLE001 - tracker is best-effort
            return 0.0

        total = completed = 0
        for stage_id in stage_ids:
            info = tracker.getStageInfo(stage_id)
            if info is None:
                continue
            total += info.numTasks
            completed += info.numCompletedTasks
        return (completed / total) if total else 0.0

    def _apply_cancel(self) -> None:
        self._cancel_requested = True
        logger.warning("cancelling spark job group %s", self._job_id)
        try:
            self._spark.sparkContext.cancelJobGroup(self._job_id)
        except Exception:  # noqa: BLE001
            logger.exception("cancelJobGroup failed for %s", self._job_id)

    def _emit(self, percent: int, phase_name: str) -> None:
        try:
            self._sink(max(0, min(int(percent), 100)), phase_name)
        except Exception:  # noqa: BLE001
            logger.debug("progress sink failed", exc_info=True)
