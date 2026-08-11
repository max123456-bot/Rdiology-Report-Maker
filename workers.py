"""
Background jobs, so a slow AI call does not freeze the page.

Market platforms pre-read a study in the background and have the draft ready
before the radiologist opens it. Streamlit has no task queue, but it does
keep imported modules alive across reruns - so a module-level thread pool
with a job table gives the same effect at this project's scale: submit the
pre-read when the file lands, keep working, collect the draft when it is
done.

Jobs are in-memory on purpose. A pre-read that dies with the process is
re-submitted in one click; a persistent queue (Celery, RQ) earns its
complexity only when there are multiple app servers, which Streamlit Cloud
does not offer anyway.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

_MAX_WORKERS = 2      # AI calls are network-bound; two keeps memory sane
_MAX_FINISHED = 50    # old results to keep before pruning


@dataclass
class Job:
    id: str
    kind: str                    # "prefill", "impression", ...
    created: str
    status: str = "running"      # running | done | failed
    result: object = None
    error: str = ""
    future: Future | None = field(default=None, repr=False)


_lock = threading.Lock()
_jobs: dict[str, Job] = {}
_pool: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=_MAX_WORKERS,
                                       thread_name_prefix="hcformat-job")
        return _pool


def _prune() -> None:
    """Drop the oldest finished jobs once there are too many. Caller holds _lock."""
    finished = [j for j in _jobs.values() if j.status != "running"]
    if len(finished) <= _MAX_FINISHED:
        return
    finished.sort(key=lambda j: j.created)
    for job in finished[:-_MAX_FINISHED]:
        _jobs.pop(job.id, None)


def submit(kind: str, fn, *args, **kwargs) -> str:
    """Run fn(*args, **kwargs) in the background. Returns a job id."""
    job = Job(
        id=uuid.uuid4().hex[:10],
        kind=kind,
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    def run():
        try:
            job.result = fn(*args, **kwargs)
            job.status = "done"
        except Exception as exc:
            job.error = f"{type(exc).__name__}: {exc}"
            job.status = "failed"

    with _lock:
        _jobs[job.id] = job
        _prune()
    job.future = _executor().submit(run)
    return job.id


def status(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def result(job_id: str):
    """The job's result. Raises while running or after failure - check status."""
    job = status(job_id)
    if job is None:
        raise KeyError(f"No job {job_id} - the process may have restarted.")
    if job.status == "running":
        raise RuntimeError("Still running.")
    if job.status == "failed":
        raise RuntimeError(job.error)
    return job.result


def running() -> list[Job]:
    with _lock:
        return [j for j in _jobs.values() if j.status == "running"]
