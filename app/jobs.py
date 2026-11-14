"""Background work. One worker thread runs jobs one at a time, so the free
API limits are never hit by two things at once, and a job keeps running
after she closes the page."""
from __future__ import annotations

import json
import queue
import threading
import traceback

from . import db
from .llm import LLMError
from .pipeline import PipelineError
from .search import SearchError

_queue: "queue.Queue[tuple[int, object]]" = queue.Queue()
_started = False
_start_lock = threading.Lock()


class JobContext:
    def __init__(self, job_id: int):
        self.job_id = job_id

    def progress(self, percent: int, message: str = "") -> None:
        values = {"progress": max(0, min(int(percent), 100))}
        if message:
            values["message"] = message
        db.update("jobs", self.job_id, values)


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, (PipelineError, LLMError, SearchError)):
        return str(exc)
    return f"Something unexpected went wrong ({exc.__class__.__name__}). Try again."


def _worker() -> None:
    while True:
        job_id, fn = _queue.get()
        db.update("jobs", job_id, {"status": "running"})
        try:
            fn(JobContext(job_id))
            db.update("jobs", job_id, {"status": "done", "progress": 100})
        except Exception as exc:  # the worker must survive anything
            if not isinstance(exc, (PipelineError, LLMError, SearchError)):
                traceback.print_exc()
            db.update("jobs", job_id, {"status": "error", "error": friendly_error(exc)})
        finally:
            _queue.task_done()


def start() -> None:
    global _started
    with _start_lock:
        if not _started:
            threading.Thread(target=_worker, name="trendpack-worker", daemon=True).start()
            _started = True


def submit(kind: str, fn, ref: dict, message: str = "Waiting to start") -> int:
    start()
    job_id = db.insert("jobs", {"created_at": db.now_iso(), "kind": kind, "status": "queued",
                                "message": message, "ref": json.dumps(ref)})
    _queue.put((job_id, fn))
    return job_id


def wait_idle() -> None:
    """For tests: block until every queued job has finished."""
    _queue.join()
