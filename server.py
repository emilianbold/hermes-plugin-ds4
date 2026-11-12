"""What a running ds4-server says about itself.

ds4-server loads exactly one GGUF and reports it on ``GET /v1/models``: every
listed id is an alias of that one model, and every entry carries the context
the server was started with (``--ctx``). That listing is the only
introspection endpoint the server has (no ``/health``, no ``/props``), so
everything Hermes needs to know about the engine comes from here.

Standard library only: the setup flow and the tests import this module without
a Hermes environment.
"""

from __future__ import annotations

import ipaddress
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
# ``owned_by`` of every ds4-server model entry; tells it apart from whatever
# else may be listening on the port.
OWNER = "ds4.c"

# A context window is a launch flag, so it only changes when the server is
# restarted. Keep answers briefly: long enough that one agent start costs one
# request, short enough that a restart with another --ctx is noticed.
_TTL_OK = 10.0
_TTL_FAILED = 3.0


@dataclass(frozen=True)
class ServedModel:
    id: str
    name: str                      # the loaded GGUF's own name
    context_length: int | None     # --ctx
    max_completion_tokens: int | None
    vision: bool | None            # None: this server build does not say


@dataclass(frozen=True)
class Probe:
    base_url: str
    models: tuple[ServedModel, ...] = ()
    error: str = ""                # why there are no models

    @property
    def ok(self) -> bool:
        return bool(self.models)

    def model(self, model_id: str) -> ServedModel | None:
        """The entry for *model_id*, else the first one.

        ds4-server answers a chat request under any model name with the GGUF it
        loaded, so an id it does not list (``deepseek-chat``, a stale config
        value) still runs with the listed context.
        """
        for entry in self.models:
            if entry.id == model_id:
                return entry
        return self.models[0] if self.models else None


def normalize_base_url(base_url: str | None) -> str:
    """The OpenAI-compatible root (``.../v1``) for a server root or a ``/v1`` URL."""
    url = (base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
    return url if url.endswith("/v1") else url + "/v1"


def _is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _vision(entry: dict) -> bool | None:
    """Image input as advertised in OpenRouter-style ``architecture`` fields."""
    architecture = entry.get("architecture")
    if not isinstance(architecture, dict):
        return None
    modalities = architecture.get("input_modalities")
    if isinstance(modalities, list):
        return "image" in modalities
    modality = architecture.get("modality")
    if isinstance(modality, str):
        return "image" in modality.split("->")[0]
    return None


def parse_models(payload: object) -> tuple[ServedModel, ...]:
    """ds4-server's entries from a ``/v1/models`` body; another server's are ignored."""
    data = payload.get("data") if isinstance(payload, dict) else None
    models = []
    for entry in data if isinstance(data, list) else ():
        if not isinstance(entry, dict) or entry.get("owned_by") != OWNER:
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        name = entry.get("name")
        top = entry.get("top_provider")
        models.append(ServedModel(
            id=model_id,
            name=name if isinstance(name, str) and name else model_id,
            context_length=_positive_int(entry.get("context_length")),
            max_completion_tokens=_positive_int(top.get("max_completion_tokens")) if isinstance(top, dict) else None,
            vision=_vision(entry),
        ))
    return tuple(models)


def fetch(base_url: str | None = None, *, api_key: str = "", timeout: float = 2.0) -> Probe:
    """Ask the server at *base_url* now. Never raises."""
    base = normalize_base_url(base_url)
    request = urllib.request.Request(base + "/models", headers={"Accept": "application/json"})
    if api_key:
        # ds4-server has no authentication; a key only matters to a proxy in front of it.
        request.add_header("Authorization", f"Bearer {api_key}")
    # A system HTTP proxy must not sit between Hermes and a loopback server.
    handlers = [urllib.request.ProxyHandler({})] if _is_loopback(base) else []
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        return Probe(base, error=f"HTTP {exc.code} from {base}/models")
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return Probe(base, error=f"no answer from {base} ({reason})")
    models = parse_models(payload)
    if not models:
        return Probe(base, error=f"the server at {base} is not ds4-server")
    return Probe(base, models=models)


_memo: dict[tuple[str, str], tuple[float, Probe]] = {}
_memo_lock = threading.Lock()


def probe(base_url: str | None = None, *, api_key: str = "", timeout: float = 2.0,
          refresh: bool = False) -> Probe:
    """``fetch`` with a short memo, for callers on Hermes' request path."""
    key = (normalize_base_url(base_url), api_key)
    now = time.monotonic()
    if not refresh:
        with _memo_lock:
            hit = _memo.get(key)
        if hit and hit[0] > now:
            return hit[1]
    result = fetch(key[0], api_key=api_key, timeout=timeout)
    with _memo_lock:
        _memo[key] = (time.monotonic() + (_TTL_OK if result.ok else _TTL_FAILED), result)
    return result


def forget() -> None:
    """Drop every memoized answer (``/model --refresh``, tests)."""
    with _memo_lock:
        _memo.clear()
