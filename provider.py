"""The ``ds4`` provider: Hermes talking to a ds4-server over its OpenAI-compatible API.

Two ways to get a server. Managed (the default once turned on in the plugin's
settings page): this plugin installed the engine, owns the model library and
starts ds4-server on the first request. Attached: the user runs ds4-server
themselves and this profile only describes how to talk to it.
"""

from __future__ import annotations

import time
from typing import Any

from providers.base import ProviderProfile

from . import runtime, server
from .setup_flow import model_setup_flow

NAME = "ds4"
ALIASES = ("dwarfstar", "ds4-server")
API_KEY_ENV = "DS4_API_KEY"
BASE_URL_ENV = "DS4_BASE_URL"
# ds4-server has no authentication. The OpenAI client still needs a non-empty
# key, and ds4's own client guide uses this string for the same purpose.
PLACEHOLDER_KEY = "dsv4-local"
# The reasoning_effort names ds4-server parses. It maps them itself: "minimal"
# runs as low, "xhigh" as high, "max" as Think Max when the context allows.
SERVER_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
# Served when no server is running and none was ever started, so ds4 can be
# selected first. Every ds4-server answers under this id whichever GGUF it loaded.
OFFLINE_MODEL = "deepseek-v4-flash"
# openai.OpenAI options core may pass to create_client; anything else is not for the SDK.
_CLIENT_OPTIONS = frozenset({"organization", "project", "timeout", "max_retries", "default_headers", "default_query"})


def _secret(name: str) -> str:
    from agent.secret_scope import get_secret  # lazy: discovery runs while core is still importing

    return (get_secret(name, "") or "").strip()


def managed() -> bool:
    return runtime.preferences()["enabled"]


def configured_base_url() -> str:
    """The endpoint in effect for this profile: the managed server's when the runtime is on,
    else ``DS4_BASE_URL``, else the ``model.base_url`` saved with a ds4 selection, else
    ds4-server's default bind."""
    if managed():
        return runtime.base_url()
    url = _secret(BASE_URL_ENV)
    if not url:
        try:
            from hermes_cli.config import load_config_readonly

            model = load_config_readonly().get("model")
        except Exception:
            model = None
        if isinstance(model, dict) and str(model.get("provider") or "").strip().lower() in (NAME, *ALIASES):
            url = str(model.get("base_url") or "").strip()
    return server.normalize_base_url(url)


def _probe(base_url: str | None = None, *, api_key: str | None = None) -> server.Probe:
    key = _secret(API_KEY_ENV) if api_key in (None, PLACEHOLDER_KEY) else api_key
    return server.probe(base_url or configured_base_url(), api_key=key)


def _window(model: str) -> int | None:
    """The context window *model* runs with: what the running server reports, else the
    launch context the managed runtime will start the active model with."""
    served = _probe().model(model)
    if served is not None:
        return served.context_length
    return runtime.launch_recipe(runtime.preferences()["active"])["ctx"] if managed() else None


def ensure_local_key() -> None:
    """Store the placeholder key, as a precaution so Hermes lists ds4 as a ready provider.

    ds4-server takes no key. Hermes' model picker once showed ds4 as "missing usable credentials"
    without one (the state that caused it is not known: a fresh profile lists ds4 as ready either
    way), so setup stores the placeholder the OpenAI client needs anyway, as oMLX stores its local
    key. Never replaces a key the user configured.
    """
    if not _secret(API_KEY_ENV):
        from hermes_cli.config import save_env_value_secure

        save_env_value_secure(API_KEY_ENV, PLACEHOLDER_KEY)


def _is_blank_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not value.strip()


class _ServerCapabilities(dict):
    """``model_capabilities`` answered by the running server instead of a static table.

    Which model a ds4-server serves, with how much context and whether it takes
    images, is decided by its launch flags. Core reads this mapping with
    ``.get(model, {})``; fields the server does not report are left out, which
    core treats as unknown.

    Coupling to Hermes: this relies on core reading ``model_capabilities`` only through
    ``.get(model, {})`` (``agent/models_dev.py``, ``_provider_model_capabilities``). If a Hermes
    release starts to iterate it or index it directly, this is the class to look at, and
    ``test_provider.py`` (the ``get_model_capabilities`` tests) is what will say so.
    """

    def get(self, model: str, default: Any = None) -> Any:  # type: ignore[override]
        capabilities: dict[str, Any] = {"supports_tools": True, "supports_reasoning": True}
        window = _window(model)
        if window:
            capabilities["context_window"] = window
        served = _probe().model(model)
        if served is not None and served.vision is not None:
            capabilities["supports_vision"] = served.vision
        return capabilities


def _start_on_demand_client(options: dict[str, Any]) -> Any:
    """An OpenAI client whose requests first make sure the managed server is up.

    The server is not kept running for its own sake: it holds the whole model
    in memory. It starts with the first request after Hermes starts, and a
    request that arrives while the model loads waits for it.
    """
    import httpx
    from openai import OpenAI

    class _Stamping(httpx.SyncByteStream):
        """A response body that keeps the idle clock fresh while it streams and when it ends."""
        def __init__(self, inner: httpx.SyncByteStream) -> None:
            self._inner = inner
            self._last = time.monotonic()

        def __iter__(self):
            for chunk in self._inner:
                if time.monotonic() - self._last > 30:
                    runtime.touch_activity()
                    self._last = time.monotonic()
                yield chunk

        def close(self) -> None:
            self._inner.close()
            runtime.touch_activity()

    class StartOnDemand(httpx.BaseTransport):
        def __init__(self) -> None:
            self._http = httpx.HTTPTransport(retries=0, trust_env=False)

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            try:
                runtime.ensure_running()
            except (OSError, RuntimeError, ValueError) as exc:
                raise httpx.ConnectError(f"The managed ds4-server could not start: {exc}", request=request) from exc
            runtime.touch_activity()
            answer = self._http.handle_request(request)
            return httpx.Response(answer.status_code, headers=answer.headers,
                                  stream=_Stamping(answer.stream), extensions=answer.extensions)

        def close(self) -> None:
            self._http.close()

    return OpenAI(
        **{key: value for key, value in options.items() if key in _CLIENT_OPTIONS},
        api_key=options.get("api_key") or PLACEHOLDER_KEY,
        base_url=runtime.base_url(),
        http_client=httpx.Client(transport=StartOnDemand(), trust_env=False, timeout=options.get("timeout")),
    )


class Ds4Profile(ProviderProfile):
    def __init__(self) -> None:
        super().__init__(
            name=NAME,
            aliases=ALIASES,
            display_name="DwarfStar (ds4)",
            description="ds4-server on this machine: DeepSeek V4 Flash and V4.1 Flash, GLM 5.3 Flash, Qwen3.8 Flash Next (no API key)",
            signup_url="https://github.com/antirez/ds4",
            env_vars=(API_KEY_ENV, BASE_URL_ENV),
            base_url=server.DEFAULT_BASE_URL,
            # Derived from base_url this would be 127.0.0.1, and core maps a
            # profile's hostname to its provider: every loopback endpoint
            # (Ollama, llama.cpp, LM Studio) would then be taken for ds4.
            hostname="ds4.invalid",
            auth_type="api_key",
            fallback_models=(OFFLINE_MODEL,),
            # An optional local server that is not running is not a fault for `hermes doctor`.
            supports_health_check=False,
            # ds4-server rejects images on tool-result messages (antirez/ds4#933).
            supports_vision=False,
            supports_vision_tool_messages=False,
            model_capabilities=_ServerCapabilities(),
            model_setup_flow=model_setup_flow,
        )

    # ── Credentials and endpoint ───────────────────────────────────────────
    def resolve_api_key_secret(self) -> tuple[str, str]:
        key = _secret(API_KEY_ENV)
        return (key, API_KEY_ENV) if key else (PLACEHOLDER_KEY, "ds4-server takes no API key")

    def runtime_base_url(self, *, api_key: str | None = None, default: str | None = None,
                         env_url: str | None = None) -> str | None:
        return runtime.base_url() if managed() else None

    def create_client(self, **client_kwargs: Any) -> Any | None:
        return _start_on_demand_client(client_kwargs) if managed() else None

    # ── Catalog ────────────────────────────────────────────────────────────
    def fetch_models(self, *, api_key: str | None = None, base_url: str | None = None,
                     timeout: float = 8.0) -> list[str] | None:
        result = _probe(None if managed() else base_url, api_key=api_key)
        if result.ok:
            return [model.id for model in result.models]
        # The managed server is off between sessions; the model it serves is still the choice.
        return runtime.preferences()["served"] or None if managed() else None

    def invalidate_catalog_cache(self) -> None:
        server.forget()

    # ── Context window ─────────────────────────────────────────────────────
    # The window is the server's --ctx: a launch flag, not a property of the
    # model id, so it changes when the server is restarted. Core asks this
    # hook before its own caches; without it a restart with another --ctx
    # keeps running on the previous launch's window.
    def get_model_context_length(self, model: str) -> int | None:
        return _window(model)

    # ── Replayed history ───────────────────────────────────────────────────
    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Send a turn that produced no reasoning back without a reasoning placeholder.

        Hermes pads such turns with ``reasoning_content: " "`` whenever the
        model id contains "deepseek", because DeepSeek's cloud API rejects a
        tool-call turn that lacks the field. ds4-server renders what it is
        given: the space becomes a token the model never generated, the
        replayed turn no longer matches the server's live KV state, and it
        prefills the whole conversation again on every such turn.
        """
        return [
            {key: value for key, value in message.items() if key != "reasoning_content"}
            if message.get("role") == "assistant" and _is_blank_text(message.get("reasoning_content"))
            else message
            for message in messages
        ]

    # ── Reasoning ──────────────────────────────────────────────────────────
    # ds4-server reads a top-level ``reasoning_effort``. With no effort
    # selected the field is left out and the server's own default (thinking
    # on) applies. A name it cannot parse is a 400, so Hermes' "ultra" goes
    # out as the server's highest level and anything else is left out.
    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, **context: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(reasoning_config, dict):
            return {}, {}
        effort = str(reasoning_config.get("effort") or "").strip().lower()
        if effort == "none" or reasoning_config.get("enabled", True) is False:
            return {}, {"reasoning_effort": "none"}
        if effort == "ultra":
            effort = "max"
        return {}, ({"reasoning_effort": effort} if effort in SERVER_EFFORTS else {})
