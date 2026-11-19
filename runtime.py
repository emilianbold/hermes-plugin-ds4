"""The managed runtime: Hermes installs the ds4 engine, keeps a model library and runs ds4-server.

Everything lives in the active profile's ``plugin-data/ds4/``:

    engine/<commit12>/   ds4-server for the pinned commit (first 12 characters of it)
    models/<id>.gguf     downloaded models, and links to files added from disk
    kv/                  ds4-server's disk KV checkpoints
    preferences.json     {enabled, active, ctx, served}
    server.json          the ds4-server this plugin started
    *.log, *.lock        the server and engine logs; the cross-process locks

Importing this module, reading status and listing models start nothing and
download nothing. ds4 publishes no binaries of its own: the engine is the
archive that this plugin's repository builds for the pinned commit in GitHub
Actions (catalog.json lists its URL, size and SHA-256). It is downloaded and
checked, never built on the user's Mac. The pin below is the one commit this
plugin was tested with.
"""

from __future__ import annotations

import atexit
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from . import server

ENGINE_COMMIT = "b3f50180ec2c7324892be1f50bc83555ccb8d5e7"
CATALOG_PATH = Path(__file__).with_name("catalog.json")

DEFAULT_CTX = 100_000          # ds4's own suggestion for coding agents
MINIMUM_CTX = 64_000           # Hermes does not run an agent on less
KV_DISK_MB = 8192
# Memory the rest of the system keeps beyond ds4's own plan for a model (the plan already counts
# the model, its caches and the context buffers). ds4 documents 96 GB Macs for the 81 GiB Flash Q2.
OS_RESERVE_BYTES = 8 * 2**30
MAX_DOWNLOAD_FAILURES = 6      # consecutive requests that made no progress
RETRY_BACKOFF_S = 2.0          # doubled per failure, up to 16x
START_TIMEOUT_S = 840          # an 80 GB model read from a cold disk takes minutes. Kept under Hermes' 900 s
                               # stale-stream limit for local servers, so our "did not finish loading" wins.
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class Cancelled(Exception):
    """A download stopped on request; the partial file is kept for resuming."""


# ── Where things live ──────────────────────────────────────────────────────
def root() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "plugin-data" / "ds4"


def engine_dir() -> Path:
    return root() / "engine" / ENGINE_COMMIT[:12]


def server_bin() -> Path:
    return engine_dir() / "ds4-server"


def models_dir() -> Path:
    return root() / "models"


def model_path(model_id: str) -> Path:
    if not _MODEL_ID.fullmatch(model_id or ""):
        raise ValueError(f"not a model id: {model_id!r}")
    return models_dir() / f"{model_id}.gguf"


def supported() -> bool:
    """The managed engine is the Metal build; other platforms attach to a server they run themselves."""
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def ram_bytes() -> int:
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")


def port() -> int:
    """One fixed loopback port per profile, so a saved endpoint stays valid across restarts."""
    return 20000 + int(hashlib.sha256(str(root()).encode()).hexdigest()[:8], 16) % 20000


def base_url() -> str:
    return f"http://127.0.0.1:{port()}/v1"


# ── Small durable state ────────────────────────────────────────────────────
def _read(name: str) -> dict:
    try:
        value = json.loads((root() / name).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(name: str, value: dict) -> None:
    root().mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{name}-", dir=root())
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle)
    os.replace(temporary, root() / name)


def preferences() -> dict:
    stored = _read("preferences.json")
    ctx = stored.get("ctx")
    served = stored.get("served")
    return {
        "enabled": stored.get("enabled") is True,
        "active": stored.get("active") if isinstance(stored.get("active"), str) else None,
        "ctx": ctx if type(ctx) is int and ctx >= MINIMUM_CTX else DEFAULT_CTX,
        # The ids the active model's server lists, remembered so the model stays
        # selectable while the server is off.
        "served": [s for s in served if isinstance(s, str)] if isinstance(served, list) else [],
    }


def set_preferences(**changes: object) -> dict:
    merged = {**preferences(), **changes}
    _write("preferences.json", merged)
    return merged


@contextmanager
def _exclusive(name: str, wait: bool = True):
    """One engine install, and one server start or stop, at a time across processes. A second
    caller waits: a request that arrives while the model loads continues once it is up. With
    *wait* False it raises BlockingIOError instead."""
    root().mkdir(parents=True, exist_ok=True)
    with open(root() / f"{name}.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# ── Engine ─────────────────────────────────────────────────────────────────
def engine_installed() -> bool:
    return server_bin().is_file()


# What ds4-server is started with. It is third-party code, and Hermes' own environment holds every
# provider key loaded from the profile's .env, so the engine gets a short list instead of a copy.
_ENGINE_ENV_NAMES = frozenset({"PATH", "HOME", "TMPDIR", "USER", "LOGNAME", "LANG"})
_ENGINE_ENV_PREFIXES = ("LC_", "DS4_", "MTL_")      # locale; ds4's own tuning switches; Metal's
_ENGINE_ENV_NEVER = frozenset({"DS4_API_KEY", "DS4_BASE_URL"})   # this plugin's own settings: a proxy's key and address


def engine_env() -> dict[str, str]:
    """The environment for ds4-server: the basics, the locale, and ds4's and Metal's own variables.
    ds4-server reads nothing else (at the pinned commit every ``getenv`` in it is a ``DS4_`` name)."""
    return {name: value for name, value in os.environ.items()
            if name not in _ENGINE_ENV_NEVER
            and (name in _ENGINE_ENV_NAMES or name.startswith(_ENGINE_ENV_PREFIXES))}


def _run(command: list[str], cwd: Path, log, timeout: int) -> None:
    log.write(f"$ {' '.join(command)}\n")
    log.flush()
    done = subprocess.run(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL, timeout=timeout, env=engine_env())
    if done.returncode:
        raise RuntimeError(f"`{' '.join(command[:2])}` failed; see {log.name}")


ENGINE_ARCHIVE_MAX_BYTES = 200 * 2**20     # the real archive is a few MB; this only bounds a hostile one


def _engine_asset() -> dict | None:
    """The engine archive catalog.json lists for the pinned commit and this Mac, if any. An entry
    for another commit is ignored: the archive must be the build of exactly what ENGINE_COMMIT pins."""
    data = json.loads(CATALOG_PATH.read_text()).get("engine") or {}
    asset = data.get("darwin-arm64") or {}
    if data.get("commit") != ENGINE_COMMIT or not asset.get("url"):
        return None
    return asset


def _unpack_engine(archive: Path, staging: Path) -> None:
    """Extract the archive's single top directory into *staging*, refusing anything but plain
    files and directories inside it."""
    unpack = staging / ".unpack"
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        tops = {Path(member.name).parts[0] for member in members if Path(member.name).parts}
        if len(tops) != 1:
            raise RuntimeError("The engine archive does not have exactly one top-level directory")
        total = 0
        for member in members:
            parts = Path(member.name).parts
            if Path(member.name).is_absolute() or ".." in parts:
                raise RuntimeError(f"The engine archive has an unsafe path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise RuntimeError(f"The engine archive has something that is not a plain file: {member.name}")
            total += member.size
        if total > 4 * ENGINE_ARCHIVE_MAX_BYTES:
            raise RuntimeError("The engine archive unpacks to far more than expected")
        tar.extractall(unpack, members=members, filter="data")
    (inner,) = (unpack / top for top in tops)
    for child in inner.iterdir():
        child.rename(staging / child.name)
    shutil.rmtree(unpack)
    archive.unlink()


def _install_archive(asset: dict, staging: Path, log, say: Callable[[str], None]) -> None:
    """Download the engine archive catalog.json lists, check its size and SHA-256, unpack it into
    *staging* and run it once. Raises on any doubt."""
    size, want = int(asset["size_bytes"]), str(asset["sha256"])
    if not 0 < size <= ENGINE_ARCHIVE_MAX_BYTES:
        raise RuntimeError("catalog.json gives the engine archive an impossible size")
    say("Downloading the ds4 engine")
    log.write(f"Downloading {asset['url']}\n")
    request = urllib.request.Request(asset["url"], headers={"User-Agent": USER_AGENT})
    archive = staging / "engine.tar.gz"
    digest, got = hashlib.sha256(), 0
    with urllib.request.urlopen(request, timeout=60) as response, open(archive, "wb") as out:
        while chunk := response.read(2**20):
            got += len(chunk)
            if got > size:
                raise RuntimeError("The engine archive is larger than catalog.json says")
            digest.update(chunk)
            out.write(chunk)
    if got != size or digest.hexdigest() != want:
        raise RuntimeError("The engine archive does not match the checksum in catalog.json")
    say("Unpacking the ds4 engine")
    _unpack_engine(archive, staging)
    binary = staging / "ds4-server"
    if not binary.is_file():
        raise RuntimeError("The engine archive has no ds4-server")
    binary.chmod(0o755)
    _run([str(binary), "--help"], staging, log, 60)


def install_engine(progress: Callable[[str], None] | None = None) -> None:
    """Install ds4-server for the pinned commit from the archive catalog.json lists: downloaded,
    checked against its size and SHA-256, unpacked and run once. A no-op when already installed.
    Any failure ends in one clear RuntimeError and leaves nothing half installed."""
    say = progress or (lambda _phase: None)
    if not supported():
        raise RuntimeError("The managed ds4 engine needs a Mac with Apple Silicon")
    if engine_installed():
        return
    asset = _engine_asset()
    if asset is None:
        raise RuntimeError("This version of the plugin lists no ds4 engine for this Mac. Update the plugin and try again.")
    with _exclusive("engine"):
        if engine_installed():
            return
        target = engine_dir()
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".build-", dir=target.parent))
        try:
            with open(root() / "engine-build.log", "w") as log:
                try:
                    _install_archive(asset, staging, log, say)
                except (OSError, ValueError, KeyError, RuntimeError, tarfile.TarError, http.client.HTTPException,
                        subprocess.SubprocessError) as exc:
                    log.write(f"The engine could not be installed: {exc}\n")
                    raise RuntimeError(f"Could not install the ds4 engine: {exc}. Check your connection and try "
                                       f"again; details are in {log.name}") from exc
            staging.rename(target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


# ── Models ─────────────────────────────────────────────────────────────────
def catalog() -> list[dict]:
    return json.loads(CATALOG_PATH.read_text())["models"]


def _catalog_entry(model_id: str) -> dict | None:
    return next((entry for entry in catalog() if entry["id"] == model_id), None)


def catalog_id_for_size(size: int) -> str | None:
    """The catalog model a file of this exact size is. Hugging Face serves the same bytes under
    several names (dated and undated, and ds4's script renames files), so size identifies a model
    where the file name does not."""
    return next((entry["id"] for entry in catalog() if entry["size_bytes"] == size), None)


def fits(resident_bytes: int) -> bool:
    """Whether a model whose memory plan is *resident_bytes* leaves the Mac room to run. File size
    is the wrong measure for ds4: a streaming model keeps a fraction of its file in memory."""
    return resident_bytes + OS_RESERVE_BYTES <= ram_bytes()


def launch_recipe(model_id: str | None) -> dict:
    """``{"ctx", "args"}`` ds4-server is started with for *model_id*: the catalog's measured recipe,
    else the context from preferences and no extra flags for a file the user added."""
    entry = _catalog_entry(model_id) if model_id else None
    if entry and entry.get("launch"):
        return {"ctx": int(entry["launch"]["ctx"]), "args": list(entry["launch"].get("args", []))}
    return {"ctx": preferences()["ctx"], "args": []}


def _recommended_id() -> str | None:
    """The catalog model suggested for this machine's memory (ds4's own hardware guide)."""
    ram_gib = ram_bytes() / 2**30
    for entry in catalog():
        lo, hi = entry.get("recommended_ram_gib") or (None, None)
        if lo is not None and ram_gib >= lo and (hi is None or ram_gib < hi):
            return entry["id"]
    return None


def library() -> list[str]:
    """Ids of the models that can be loaded now: finished downloads and linked files."""
    if not models_dir().is_dir():
        return []
    return sorted(p.stem for p in models_dir().glob("*.gguf") if p.exists() and _MODEL_ID.fullmatch(p.stem))


def _download_url(entry: dict) -> str:
    """The file at the repo revision the size and checksum were read from, so a file replaced
    upstream later cannot turn an 80 GB download into a checksum failure."""
    return entry.get("url") or f"https://huggingface.co/{entry['repo']}/resolve/{entry.get('revision') or 'main'}/{entry['file']}"


def _wait(seconds: float, cancel: threading.Event | None) -> None:
    if cancel is not None:
        cancel.wait(seconds)
    else:
        time.sleep(seconds)


def _plugin_version() -> str:
    """The version in plugin.yaml, so the User-Agent cannot drift from the release."""
    try:
        match = re.search(r'^version:\s*"?([\w.+-]+)', (Path(__file__).parent / "plugin.yaml").read_text(), re.M)
    except OSError:
        match = None
    return match.group(1) if match else "dev"


# Says who is downloading, so a host can tell this plugin's traffic from a script's.
USER_AGENT = f"hermes-plugin-ds4/{_plugin_version()} (Hermes Agent plugin; unofficial; +https://github.com/emilianbold/hermes-plugin-ds4)"


def download(model_id: str, progress: Callable[[int, int], None] | None = None,
             cancel: threading.Event | None = None,
             checking: Callable[[bool], None] | None = None) -> None:
    """Download a catalog model, resuming a partial file. The result is published only when
    its size and SHA-256 match the catalog.

    *checking(True)* is called while a partial file from an earlier attempt is re-read to rebuild
    its checksum (tens of seconds for tens of GB), *checking(False)* when that is over.

    One download of a model runs at a time, across threads and across processes: two writers on one
    partial file corrupt it, and a checksum failure deletes it (80 to 365 GB lost). A second caller
    waits, then finds the model finished and returns.
    """
    entry = _catalog_entry(model_id)
    if entry is None:
        raise ValueError(f"{model_id} is not in the ds4 catalog")
    final = model_path(model_id)
    models_dir().mkdir(parents=True, exist_ok=True)
    with _exclusive(f"download-{model_id}"):
        if not final.exists():
            _fetch(entry, final, progress, cancel, checking)


def _fetch(entry: dict, final: Path, progress: Callable[[int, int], None] | None,
           cancel: threading.Event | None, checking: Callable[[bool], None] | None) -> None:
    """The download itself, under the model's lock.

    A dropped connection or an expired CDN link ends one request, not the download: it is retried
    from the last byte received, asking the host again for a fresh link.
    """
    model_id = entry["id"]
    total = int(entry["size_bytes"])
    partial = final.with_suffix(".gguf.part")
    done = partial.stat().st_size if partial.exists() else 0
    if done > total:
        partial.unlink()
        done = 0
    if shutil.disk_usage(models_dir()).free < total - done + 2**30:
        raise RuntimeError(f"Not enough free disk space for {entry['name']}: free up about "
                           f"{(total - done + 2**30) / 2**30:.0f} GB and try again")

    def stopped() -> bool:
        return cancel is not None and cancel.is_set()

    digest = hashlib.sha256()
    with open(partial, "ab+") as out:
        out.seek(0)
        if done and checking:
            checking(True)      # hashing tens of GB takes a while: let the page say so
        try:
            while chunk := out.read(2**22):       # a resumed file is hashed from its start
                if stopped():
                    raise Cancelled(model_id)
                digest.update(chunk)
        finally:
            if done and checking:
                checking(False)
        failures = 0
        while done < total:
            request = urllib.request.Request(_download_url(entry),
                                             headers={"Range": f"bytes={done}-", "User-Agent": USER_AGENT})
            before = done
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    if response.status != 206 and done:
                        raise RuntimeError("The download host does not resume; delete the partial file to start over")
                    while chunk := response.read(2**20):
                        if stopped():
                            raise Cancelled(model_id)
                        out.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 404, 416):   # 403 is what an expired signed link answers: ask again
                    raise RuntimeError(f"Hugging Face answered {exc.code} for {entry['file']}. "
                                       "The file may have moved: check for a plugin update") from exc
                error: Exception | None = exc
            except (OSError, http.client.HTTPException) as exc:   # dropped connection, timeout, short body
                error = exc
            else:
                error = None
            out.flush()
            if done >= total:
                break
            # A request that brought no new bytes is a failure whatever the host said: an empty
            # 200 or 206 must not spin this loop at full speed.
            failures = 0 if done > before else failures + 1
            if failures >= MAX_DOWNLOAD_FAILURES:
                raise RuntimeError(f"The download of {entry['name']} keeps failing "
                                   f"({error or 'the host sent no data'}); "
                                   "start it again to resume from where it stopped") from error
            _wait(RETRY_BACKOFF_S * 2 ** min(failures, 4), cancel)
            if stopped():
                raise Cancelled(model_id)
    if done != total or digest.hexdigest() != entry["sha256"]:
        partial.unlink()
        raise RuntimeError(f"{entry['name']} did not match its checksum; the download was discarded")
    os.replace(partial, final)


def link(path: str) -> str:
    """Add a GGUF that is already on disk. The library gets a link; the file stays where it is."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"No such file: {source}")
    with open(source, "rb") as handle:
        if handle.read(4) != b"GGUF":
            raise ValueError(f"{source.name} is not a GGUF file")
    known = catalog_id_for_size(source.stat().st_size)
    model_id = known or re.sub(r"[^a-z0-9._-]+", "-", source.stem.lower()).strip("-.")[:64]
    target = model_path(model_id)
    if target.exists() or target.is_symlink():
        if target.resolve() == source:
            return model_id
        raise ValueError(f"The library already has a model named {model_id}")
    models_dir().mkdir(parents=True, exist_ok=True)
    target.symlink_to(source)
    return model_id


def delete(model_id: str) -> None:
    """Remove a model from the library. A linked file is unlinked, never deleted."""
    state = running()
    if state and state["model"] == model_id:
        raise RuntimeError("Turn the server off before deleting the model it is running")
    target = model_path(model_id)
    if target.is_symlink() or target.exists():
        target.unlink()
    discard_partial(model_id)
    if preferences()["active"] == model_id:
        set_preferences(active=None)


def discard_partial(model_id: str) -> None:
    """Delete the unfinished download of a model, unless one is running right now."""
    partial = model_path(model_id).with_suffix(".gguf.part")
    try:
        with _exclusive(f"download-{model_id}", wait=False):
            partial.unlink(missing_ok=True)
    except BlockingIOError:
        raise RuntimeError("This model is still downloading. Pause the download first.") from None


# ── Storage ────────────────────────────────────────────────────────────────
# Hermes keeps plugin-data/<plugin>/ when a plugin is removed (there is no uninstall hook), so what
# this plugin stored (tens of GB of models, a prompt cache with your prompts in it) would stay behind.
# The page shows it and offers to clear it.
def _size(path: Path) -> int:
    """Bytes of the files under *path*. A link counts for nothing: a linked model is the user's file."""
    if path.is_symlink() or not path.exists():
        return 0
    if path.is_file():
        return path.lstat().st_size
    total = 0
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            total += info.st_size if stat.S_ISREG(info.st_mode) else 0
    return total


def storage() -> dict:
    """What this plugin keeps on disk, in bytes: the engine, models Hermes downloaded (linked files
    excluded), the prompt cache and the logs."""
    return {"engine": _size(root() / "engine"), "models": _size(models_dir()), "cache": _size(root() / "kv"),
            "logs": _size(root() / "server.log") + _size(root() / "engine-build.log")}


def _empty(path: Path) -> None:
    """Delete what is inside *path* and the directory itself. Links are removed, never followed."""
    if path.is_symlink():
        path.unlink()
        return
    if not path.is_dir():
        return
    for child in path.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)
    path.rmdir()


def _remove_downloaded_models(*, links_too: bool) -> int:
    """Delete the model files in the library (finished downloads and unfinished ``.part`` files) and
    return the bytes freed. Every regular file in the folder goes, whoever put it there: the plugin
    owns the folder and downloads are the supported way in; a file the user wants kept is added with
    ``link``. A link is theirs: it goes only when *links_too*, and its target never does."""
    freed = 0
    if not models_dir().is_dir():
        return 0
    for child in list(models_dir().iterdir()):
        if child.is_symlink():
            if links_too:
                child.unlink()
        elif child.is_file():
            freed += child.stat().st_size
            child.unlink()
    if links_too:
        _empty(models_dir())
    return freed


STORAGE_KINDS = ("cache", "models", "engine", "all")


def clear_storage(what: str) -> int:
    """Delete one kind of stored data and return the bytes freed.

    ``cache``: ds4's prompt cache (it holds prompt text; ds4 wants the server stopped first).
    ``models``: the models this plugin downloaded; files the user added are only ever unlinked.
    ``engine``: ds4-server; setup installs it again.
    ``all``: all of that plus logs and preferences, so uninstalling the plugin leaves nothing behind
    (Hermes keeps a plugin's data when it is removed and has no uninstall hook).
    The server is stopped first; models and engine are refused while a download or install runs."""
    if what not in STORAGE_KINDS:
        raise ValueError(f"unknown kind of data: {what}")
    with contextlib.ExitStack() as busy:
        try:
            if what in ("engine", "all"):
                busy.enter_context(_exclusive("engine", wait=False))
            if what in ("models", "all"):
                for entry in catalog():
                    busy.enter_context(_exclusive(f"download-{entry['id']}", wait=False))
        except BlockingIOError:
            raise RuntimeError("An install or a download is running. Pause it first.") from None
        with _exclusive("server"):
            state = running()
            if state:
                _stop(state)
            freed = 0
            if what in ("cache", "all"):
                freed += _size(root() / "kv")
                _empty(root() / "kv")
            if what in ("engine", "all"):
                freed += _size(root() / "engine")
                _empty(root() / "engine")
            if what in ("models", "all"):
                freed += _remove_downloaded_models(links_too=what == "all")
            if what == "all":
                for name in ("server.log", "engine-build.log"):
                    freed += _size(root() / name)
                for name in ("server.log", "engine-build.log", "preferences.json", "server.json", "activity"):
                    (root() / name).unlink(missing_ok=True)
            elif what == "engine":
                set_preferences(enabled=False)       # nothing can load without the engine
            elif preferences()["active"] not in library():
                # The default model was one of those removed: it is not the default any more and the
                # runtime goes back to off. A linked file survives, and so does its place as the default.
                set_preferences(enabled=False, active=None)
    if what == "all":
        # The locks are released now. Without them and with every folder emptied, the plugin's own
        # folder is empty: remove it, so that uninstalling the plugin leaves nothing behind.
        for lock in root().glob("*.lock"):
            lock.unlink(missing_ok=True)
        try:
            root().rmdir()
        except OSError:
            pass                                 # something else lives there: not ours to delete
    return freed


# ── Server ─────────────────────────────────────────────────────────────────
def running() -> dict | None:
    """The ds4-server this plugin started, if that very process is still alive."""
    import psutil

    state = _read("server.json")
    try:
        process = psutil.Process(state["pid"])
        # The start time tells our process from a later one that reused its pid.
        if process.create_time() != state["create_time"] or process.status() == psutil.STATUS_ZOMBIE:
            return None
    except (KeyError, TypeError, psutil.Error):
        return None
    return state


def _log_tail(lines: int = 6) -> str:
    try:
        return "\n".join((root() / "server.log").read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def start(model_id: str | None = None) -> dict:
    """Run ds4-server with *model_id* (default: the active model) and wait until it serves."""
    import psutil

    model_id = model_id or preferences()["active"]
    if not model_id:
        raise RuntimeError("Choose a model first")
    if not engine_installed():
        raise RuntimeError("Install the ds4 engine first")
    model = model_path(model_id)
    if not model.exists():
        raise RuntimeError(f"{model_id} is not in the model library")
    with _exclusive("server"):
        state = running()
        if state and state["model"] == model_id:
            touch_activity()       # taking over a running server: its idle clock starts now
            _watch_idle(state)     # one this process did not start (it survived a hard kill) is unloaded too
            return state
        if state:
            _stop(state)
        recipe = launch_recipe(model_id)
        ctx = recipe["ctx"]
        command = [str(server_bin()), "-m", str(model.resolve()), "--ctx", str(ctx), *recipe["args"],
                   "--host", "127.0.0.1", "--port", str(port()), "--chdir", str(engine_dir()),
                   "--kv-disk-dir", str(root() / "kv"), "--kv-disk-space-mb", str(KV_DISK_MB)]
        (root() / "kv").mkdir(parents=True, exist_ok=True)
        with open(root() / "server.log", "ab") as log:
            log.write(f"\n$ {' '.join(command)}\n".encode())
            log.flush()
            child = subprocess.Popen(command, cwd=engine_dir(), stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=log, start_new_session=True, env=engine_env())
        state = {"pid": child.pid, "create_time": psutil.Process(child.pid).create_time(),
                 "model": model_id, "ctx": ctx, "base_url": base_url()}
        _write("server.json", state)
        atexit.register(_stop_if_ours, state)
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if child.poll() is not None:
                (root() / "server.json").unlink(missing_ok=True)
                raise RuntimeError(f"ds4-server exited while loading {model_id} "
                                   f"(log: {root() / 'server.log'}):\n{_log_tail()}")
            if server.fetch(base_url(), timeout=2.0).ok:
                server.forget()
                touch_activity()          # the idle clock starts when the model is ready, not when loading began
                _watch_idle(state)
                return state
            time.sleep(0.5)
        _stop(state)
        raise RuntimeError(f"ds4-server did not finish loading {model_id} in {START_TIMEOUT_S} s "
                           f"(log: {root() / 'server.log'})")


def _stop(state: dict) -> None:
    import psutil

    try:
        process = psutil.Process(state["pid"])
        if process.create_time() == state["create_time"]:
            process.terminate()
            try:
                process.wait(timeout=20)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    except psutil.Error:
        pass
    (root() / "server.json").unlink(missing_ok=True)
    server.forget()


def _stop_if_ours(state: dict) -> None:
    """At exit of the process that started the server: stop it unless it was already replaced."""
    current = _read("server.json")
    if current.get("pid") == state["pid"] and current.get("create_time") == state["create_time"]:
        _stop(state)


def stop() -> None:
    with _exclusive("server"):
        state = running()
        if state:
            _stop(state)


# ── Idle unload ────────────────────────────────────────────────────────────
# Hermes unloads its llama.cpp models after 15 idle minutes so their memory comes back; a ds4 model
# holds 80+ GiB, so this matters more. ds4-server cannot say whether it is busy, so the requests
# Hermes makes through this plugin are the clock: one is stamped when it starts, while its answer
# streams, and when it ends. "Turn off" is not involved: the model comes back with the next message.
IDLE_UNLOAD_S = 15 * 60
IDLE_CHECK_S = 30.0


def touch_activity() -> None:
    root().mkdir(parents=True, exist_ok=True)
    (root() / "activity").touch()


def idle_seconds() -> float:
    try:
        return max(0.0, time.time() - (root() / "activity").stat().st_mtime)
    except OSError:
        return 0.0


_watching: set[int] = set()     # pids of servers this process already watches


def _watch_idle(state: dict) -> None:
    """Stop *state*'s server once it has had no request for IDLE_UNLOAD_S. Lives with this process
    (which also stops the server at exit) and ends when the server is gone or replaced. One watcher
    per server, however often this is called."""
    pid = state["pid"]
    if pid in _watching:
        return
    _watching.add(pid)

    def watch() -> None:
        try:
            while True:
                time.sleep(IDLE_CHECK_S)
                current = running()
                if current is None or current["pid"] != pid:
                    return
                if idle_seconds() < IDLE_UNLOAD_S:
                    continue
                with _exclusive("server"):
                    current = running()
                    if current is None or current["pid"] != pid:
                        return
                    if idle_seconds() >= IDLE_UNLOAD_S:      # looked again under the lock
                        _stop(current)
                        return
        finally:
            _watching.discard(pid)

    threading.Thread(target=watch, name="ds4-idle-unload", daemon=True).start()


def activate(model_id: str) -> dict:
    """Load *model_id* and make it the model the runtime serves from now on. Returns the
    running server's state plus ``served``: the model ids it answers under."""
    state = start(model_id)
    served = [model.id for model in server.fetch(base_url()).models]
    if not served:      # it answered a moment ago: say what is wrong rather than save an empty answer
        raise RuntimeError("ds4-server started, but did not say which model it serves. Try again.")
    set_preferences(enabled=True, active=model_id, served=served)
    return {**state, "served": served}


def ensure_running() -> None:
    """Before a request: bring the server up when the user turned the runtime on and it is not.

    A client built while the runtime was on outlives "Turn off"; without this check its next
    message would quietly load an 80+ GiB model again."""
    if running() is not None:
        return
    if not preferences()["enabled"]:
        raise RuntimeError("The managed ds4-server is turned off. "
                           "Turn it on in Settings → Plugins → Local Models · DwarfStar, or pick another model.")
    start()


# ── What the settings page shows ───────────────────────────────────────────
def status() -> dict:
    prefs = preferences()
    state = running()
    ram = ram_bytes()
    have = set(library())
    by_catalog = {entry["id"]: entry for entry in catalog()}
    recommended = _recommended_id()
    models = []
    for model_id in [*by_catalog, *sorted(have - set(by_catalog))]:
        entry = by_catalog.get(model_id, {})
        path = model_path(model_id)
        partial = path.with_suffix(".gguf.part")
        size = path.stat().st_size if model_id in have else entry.get("size_bytes", 0)
        resident = int(entry["resident_gib"] * 2**30) if entry.get("resident_gib") else None
        models.append({
            "id": model_id, "name": entry.get("name", model_id),
            "description": entry.get("description", ""),
            # Where a catalog model comes from, shown on hover.
            "origin": entry.get("origin"), "repo": entry.get("repo"), "license": entry.get("license"),
            "size_bytes": size,
            # A file the user added has no memory plan to judge: it is never blocked.
            "fit_known": resident is not None, "fits": True if resident is None else fits(resident),
            "resident_bytes": resident, "streaming": bool(entry.get("streaming")),
            "launch_ctx": launch_recipe(model_id)["ctx"] if entry else None,
            "state": ("linked" if path.is_symlink() else "downloaded") if model_id in have else "available",
            # Where a linked file really lives: the library only points at it.
            "path": str(path.resolve()) if model_id in have and path.is_symlink() else None,
            "partial_bytes": partial.stat().st_size if partial.exists() else 0,
            "active": prefs["active"] == model_id,
            "recommended": recommended == model_id,
            "catalog": model_id in by_catalog,
        })
    return {
        "supported": supported(), "ram_bytes": ram, "enabled": prefs["enabled"], "ctx": prefs["ctx"],
        "engine": {"installed": engine_installed(), "commit": ENGINE_COMMIT[:12]},
        "server": {"running": state is not None, "model": state["model"] if state else None,
                   "base_url": base_url(), "ctx": state["ctx"] if state else None},
        "models": models, "models_dir": str(models_dir()), "storage": storage(),
    }
