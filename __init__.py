"""Provider Endpoint Healer (PEH): Hermes plugin entry point.

Uses public plugin APIs only: ``ctx.get_config``, ``ctx.register_hook`` and
``ctx.register_command``. No Hermes internals are imported or patched.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

try:
    from .src.healer import DEFAULT_CAPABILITY, Endpoint, Healer
except ImportError:  # imported as a bare module (pytest imports the repo root this way)
    from src.healer import DEFAULT_CAPABILITY, Endpoint, Healer

logger = logging.getLogger(__name__)

_healer: Optional[Healer] = None
_probe_on_session_start = True


def _load_healer(ctx) -> Healer:
    """Build a Healer from this plugin's settings (``plugins.entries.<id>.settings``)."""
    raw = ctx.get_config("endpoints", default=[]) or []
    endpoints = []
    if not isinstance(raw, list):
        logger.warning("PEH: 'endpoints' must be a list; ignoring it")
        raw = []
    for item in raw:
        try:
            endpoints.append(Endpoint.from_dict(item))
        except (TypeError, ValueError) as exc:
            logger.warning("PEH: skipping invalid endpoint: %s", exc)
    policy = {
        "primary": ctx.get_config("primary", default="") or None,
        "cooldown_s": ctx.get_config("cooldown_s", default=60),
        "probe_timeout_s": ctx.get_config("probe_timeout_s", default=3),
    }
    return Healer(endpoints, policy)


def _on_session_start(**kwargs) -> None:
    """Probe in a short-lived daemon thread so session start is never blocked."""
    healer = _healer
    if healer is None or not healer.endpoints or not _probe_on_session_start:
        return
    threading.Thread(target=healer.probe_all, name="peh-probe", daemon=True).start()


def _handle_peh(raw_args: str = "") -> str:
    healer = _healer
    if healer is None:
        return "PEH is not initialized."
    if not healer.endpoints:
        return (
            "PEH has no endpoints configured. Add them under "
            "plugins.entries.provider-endpoint-healer.settings.endpoints in config.yaml "
            "(see examples/config.yaml)."
        )
    capability = (raw_args or "").strip() or DEFAULT_CAPABILITY
    healer.probe_all()

    lines = [f"PEH status (capability: {capability})"]
    for ep in healer.endpoints.values():
        if ep.healthy is None:
            state = "unknown"
        elif ep.healthy:
            state = "reachable"
        else:
            state = "DOWN"
        detail = f"HTTP {ep.last_status}" if ep.last_status is not None else (ep.last_error or "no response")
        marker = " (primary)" if ep.id == healer.primary else ""
        lines.append(
            f"- {ep.id}{marker}: {state}, {detail}, {ep.latency_ms:.0f} ms, "
            f"cost/1k={ep.cost_per_1k}, caps={','.join(ep.capabilities)}"
        )
    ranked = healer.rank_candidates(capability)
    if ranked:
        best = ranked[0]
        lines.append(f"Recommended: {best.id} ({best.latency_ms:.0f} ms, cost/1k={best.cost_per_1k})")
    else:
        lines.append(f"No endpoint available for '{capability}' (all unsupported or cooling down).")
    lines.append("PEH only reports. It does not switch Hermes providers.")
    return "\n".join(lines)


def register(ctx) -> None:
    global _healer, _probe_on_session_start
    _healer = _load_healer(ctx)
    _probe_on_session_start = bool(ctx.get_config("probe_on_session_start", default=True))
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_command("peh", _handle_peh, description="Provider Endpoint Healer: endpoint health status")
    logger.info("PEH registered with %d endpoint(s)", len(_healer.endpoints))
