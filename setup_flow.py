"""``hermes model`` → DwarfStar (ds4): check the server, pick its model, save."""

from __future__ import annotations

import sys
from typing import Any

from . import runtime, server

# Hermes does not run an agent on a smaller context window than this.
MINIMUM_CONTEXT = 64_000

START_HINT = (
    "Start it from your ds4 checkout, for example:\n"
    "  ./ds4-server --ctx 100000 --kv-disk-dir ~/.ds4/server-kv --kv-disk-space-mb 8192\n"
    "or let Hermes run it: Hermes Desktop → Settings → Plugins → Local Models · DwarfStar."
)


def describe(result: server.Probe) -> list[str]:
    """What the setup step prints about the server it found (or did not find)."""
    if not result.ok:
        return [f"ds4-server: {result.error}.", START_HINT, f"Hermes needs --ctx {MINIMUM_CONTEXT} or more."]
    first = result.models[0]
    lines = [f"ds4-server at {result.base_url}: {first.name}, context {first.context_length or 'unknown'} tokens."]
    if first.context_length and first.context_length < MINIMUM_CONTEXT:
        lines.append(
            f"Hermes needs a context of {MINIMUM_CONTEXT} tokens or more: "
            f"restart ds4-server with --ctx {MINIMUM_CONTEXT} or higher."
        )
    return lines


def _choose(models: list[str], current: str) -> str:
    default = current if current in models else models[0]
    if len(models) == 1 or not sys.stdin.isatty():
        return default
    for index, model in enumerate(models, 1):
        print(f"  {index}. {model}{'  (current)' if model == default else ''}")
    try:
        answer = input(f"Model [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    if not answer:
        return default
    if answer.isdigit() and 1 <= int(answer) <= len(models):
        return models[int(answer) - 1]
    # Any name is accepted: ds4-server answers with its loaded model, and names
    # such as "deepseek-chat" select its non-thinking mode.
    return answer


def model_setup_flow(config: Any, current_model: str, args: Any) -> None:
    from hermes_cli.model_setup import persist_model_selection

    from .provider import NAME, OFFLINE_MODEL, configured_base_url, ensure_local_key, managed

    ensure_local_key()
    base_url = configured_base_url()
    result = server.probe(base_url, refresh=True)
    served = runtime.preferences()["served"] if managed() else []
    if managed() and not result.ok:
        # The managed server holds the model in memory only while Hermes uses it.
        print("The managed ds4-server is off; Hermes starts it with your first message.")
    else:
        for line in describe(result):
            print(line)
    models = [model.id for model in result.models] or served or [OFFLINE_MODEL]
    if not result.ok and not served:
        print(f"Saving {OFFLINE_MODEL}: every ds4-server answers under that name once it is running.")
    selected = _choose(models, current_model)
    if not selected:
        print("No change.")
        return
    persist_model_selection(selected, NAME, base_url=base_url, api_mode="chat_completions")
    print(f"Default model set to: {selected} (via DwarfStar · ds4-server at {base_url})")
