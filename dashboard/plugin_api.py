"""Backend of the DwarfStar settings page, mounted by Hermes at ``/api/plugins/ds4/``.

Thin routes over ``runtime.py``. Long operations (building the engine,
downloading a model, loading it) run as background jobs the page polls.
"""

from __future__ import annotations

import collections
import threading
import time
import uuid
from contextvars import copy_context
from importlib import import_module

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

RATE_WINDOW_S = 10   # a download's speed is averaged over this many seconds


def _profile():
    from providers import get_provider_profile

    profile = get_provider_profile("ds4")
    if profile is None:
        raise HTTPException(503, "The ds4 provider is not loaded")
    return profile


def _runtime():
    """The provider's own ``runtime`` module. Hermes loads this file on its own, outside the
    plugin package, so the package is reached through the profile it registered."""
    return import_module(type(_profile()).__module__.rsplit(".", 1)[0] + ".runtime")


def _save_default_model(model: str, base_url: str) -> None:
    from hermes_cli.model_setup import persist_model_selection

    # A placeholder key, as a precaution: see ensure_local_key.
    import_module(type(_profile()).__module__).ensure_local_key()
    persist_model_selection(model, "ds4", base_url=base_url, api_mode="chat_completions")


class ModelBody(BaseModel):
    model_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")


class PathBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class JobBody(BaseModel):
    job_id: str


class StorageBody(BaseModel):
    what: str = Field(pattern=r"^(cache|models|engine|all)$")


class Jobs:
    """Background operations of this process, newest last. A restarted Hermes starts with none:
    a download it interrupted shows as a partial file and resumes from the model's row."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._cancel: dict[str, threading.Event] = {}

    def listing(self) -> list[dict]:
        with self._lock:
            return [dict(job) for job in self._jobs.values()]

    def cancel(self, job_id: str) -> None:
        with self._lock:
            event = self._cancel.get(job_id)
        if event is None:
            raise HTTPException(409, "This operation can no longer be cancelled")
        event.set()

    def spawn(self, kind: str, model_id: str | None, action) -> dict:
        """Run ``action(job, cancel)`` on a thread; ``job`` is the dict the page sees."""
        job = {"id": uuid.uuid4().hex, "kind": kind, "model_id": model_id, "status": "running", "phase": kind,
               "message": "", "done_bytes": 0, "total_bytes": 0, "percent": None, "bytes_per_sec": None,
               "eta_seconds": None, "can_pause": False, "error": None}
        cancel = threading.Event()
        with self._lock:
            if any(j["status"] == "running" and (j["kind"], j["model_id"]) == (kind, model_id)
                   for j in self._jobs.values()):
                raise HTTPException(409, "That is already in progress")
            for old in [k for k, j in self._jobs.items() if j["status"] != "running"][:-20]:
                del self._jobs[old]
            self._jobs[job["id"]] = job
            self._cancel[job["id"]] = cancel

        def run() -> None:
            try:
                action(job, cancel)
                job["status"] = "done"
            except Exception as exc:  # the page shows whatever went wrong
                cancelled = type(exc).__name__ == "Cancelled"
                job.update(status="cancelled" if cancelled else "error", error=None if cancelled else str(exc))
            finally:
                with self._lock:
                    self._cancel.pop(job["id"], None)

        # The request's context carries the profile this operation belongs to.
        threading.Thread(target=copy_context().run, args=(run,), name=f"ds4-{kind}", daemon=True).start()
        return dict(job)


def create_router(runtime=None, save_default_model=_save_default_model) -> APIRouter:
    jobs = Jobs()
    api = APIRouter()

    def rt():
        return runtime or _runtime()

    def guarded(call):
        try:
            return call()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (RuntimeError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    def download(job: dict, cancel: threading.Event, model_id: str) -> None:
        job.update(phase="downloading", message="Downloading", can_pause=True)
        samples: collections.deque = collections.deque()    # (time, bytes done) over the last few seconds

        def progress(done: int, total: int) -> None:
            now = time.monotonic()
            samples.append((now, done))
            while len(samples) > 1 and now - samples[0][0] > RATE_WINDOW_S:
                samples.popleft()
            elapsed = now - samples[0][0]
            rate = (done - samples[0][1]) / elapsed if len(samples) > 1 and elapsed > 0 else None
            job.update(done_bytes=done, total_bytes=total, percent=100 * done / total if total else None,
                       bytes_per_sec=rate, eta_seconds=(total - done) / rate if rate else None)

        try:
            rt().download(model_id, progress=progress, cancel=cancel,
                          checking=lambda active: job.update(checking=active))
        finally:
            job.update(can_pause=False, bytes_per_sec=None, eta_seconds=None, checking=False)

    def install(job: dict) -> None:
        job.update(phase="engine", message="Installing the engine")
        rt().install_engine(lambda phase: job.update(message=phase))

    def use(job: dict, model_id: str) -> None:
        job.update(phase="starting-server", message="Loading the model. The first load can take a few minutes.")
        state = rt().activate(model_id)
        save_default_model(state["served"][0], state["base_url"])

    @api.get("/status")
    def status():
        return {**guarded(rt().status), "jobs": jobs.listing()}

    @api.post("/engine/install")
    def engine_install():
        return jobs.spawn("install", None, lambda job, cancel: install(job))

    @api.post("/models/download")
    def models_download(body: ModelBody):
        return jobs.spawn("download", body.model_id, lambda job, cancel: download(job, cancel, body.model_id))

    @api.post("/models/link")
    def models_link(body: PathBody):
        return {"model_id": guarded(lambda: rt().link(body.path))}

    @api.post("/models/delete")
    def models_delete(body: ModelBody):
        guarded(lambda: rt().delete(body.model_id))
        return {"ok": True}

    @api.post("/use")
    def use_model(body: ModelBody):
        return jobs.spawn("use", body.model_id, lambda job, cancel: use(job, body.model_id))

    @api.post("/quickstart")
    def quickstart(body: ModelBody):
        """Everything from nothing: install the engine, download the model, load it, make it the default."""
        def action(job: dict, cancel: threading.Event) -> None:
            install(job)
            if body.model_id not in rt().library():
                download(job, cancel, body.model_id)
            use(job, body.model_id)
        return jobs.spawn("quickstart", body.model_id, action)

    @api.post("/server/start")
    def server_start():
        def action(job: dict, cancel: threading.Event) -> None:
            job["message"] = "Loading the model. The first load can take a few minutes."
            rt().set_preferences(enabled=True)
            rt().start()
        return jobs.spawn("start", None, action)

    @api.post("/server/stop")
    def server_stop():
        # Off stays off: no request starts the server again until it is turned on.
        guarded(lambda: (rt().set_preferences(enabled=False), rt().stop()))
        return {"ok": True}

    @api.post("/server/eject")
    def server_eject():
        """Unload the model to free its memory. Unlike Turn off, the next message loads it again."""
        guarded(lambda: rt().stop())
        return {"ok": True}

    @api.post("/storage/clear")
    def storage_clear(body: StorageBody):
        """Delete one kind of stored data (or all of it) and say how much that freed. Hermes keeps a
        plugin's data when the plugin is uninstalled, so this is how to clean up first."""
        return {"freed_bytes": guarded(lambda: rt().clear_storage(body.what))}

    @api.post("/jobs/cancel")
    def jobs_cancel(body: JobBody):
        jobs.cancel(body.job_id)
        return {"ok": True}

    return api


router = create_router()
