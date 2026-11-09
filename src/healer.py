"""Provider Endpoint Healer core: probing, ranking and cooldown.

Pure Python, no Hermes imports. The plugin entry point (``__init__.py``) wires
this into Hermes through public plugin APIs only.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_CAPABILITY = "chat"
DEFAULT_COOLDOWN_S = 60.0
DEFAULT_PROBE_TIMEOUT_S = 3.0
MAX_PROBE_WORKERS = 8
USER_AGENT = "provider-endpoint-healer/0.1"


@dataclass
class Endpoint:
    """A model endpoint to watch, plus its runtime health state."""

    id: str
    base_url: str
    provider: str = ""
    model: str = ""
    capabilities: List[str] = field(default_factory=lambda: [DEFAULT_CAPABILITY])
    cost_per_1k: float = 0.0

    # Runtime state (never read from config).
    healthy: Optional[bool] = None  # None = not probed yet
    last_status: Optional[int] = None
    last_error: str = ""
    latency_ms: float = 0.0  # latency of the most recent probe
    cooldown_until: float = 0.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Endpoint":
        """Build an endpoint from a config mapping.

        Unknown keys are ignored and runtime-state keys are not accepted, so a
        config typo cannot break loading of the other endpoints. Raises
        ``ValueError`` for an entry that cannot be used.
        """
        if not isinstance(data, dict):
            raise ValueError("endpoint entry must be a mapping")
        runtime = {"healthy", "last_status", "last_error", "latency_ms", "cooldown_until"}
        known = {f.name for f in fields(cls)} - runtime
        kwargs = {k: v for k, v in data.items() if k in known}

        ep_id = str(kwargs.get("id") or "").strip()
        base_url = str(kwargs.get("base_url") or "").strip()
        if not ep_id:
            raise ValueError("endpoint is missing 'id'")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"endpoint {ep_id!r}: base_url must be an http(s) URL")
        kwargs["id"] = ep_id
        kwargs["base_url"] = base_url

        caps = kwargs.get("capabilities")
        if caps is None:
            kwargs.pop("capabilities", None)
        elif isinstance(caps, str):
            kwargs["capabilities"] = [caps]
        else:
            kwargs["capabilities"] = [str(c) for c in caps]
        try:
            kwargs["cost_per_1k"] = float(kwargs.get("cost_per_1k") or 0.0)
        except (TypeError, ValueError):
            raise ValueError(f"endpoint {ep_id!r}: cost_per_1k must be a number")
        return cls(**kwargs)


class Healer:
    """Tracks endpoint health and recommends the best candidate.

    PEH only observes and recommends. It does not change which provider
    Hermes uses: Hermes has no public plugin route for that.
    """

    def __init__(self, endpoints: Iterable[Endpoint], policy: Optional[Dict[str, Any]] = None):
        self.endpoints: Dict[str, Endpoint] = {e.id: e for e in endpoints}
        self.policy: Dict[str, Any] = dict(policy or {})
        self.primary: Optional[str] = self.policy.get("primary") or None
        self._lock = threading.Lock()

    @property
    def cooldown_s(self) -> float:
        return _positive(self.policy.get("cooldown_s"), DEFAULT_COOLDOWN_S)

    @property
    def probe_timeout_s(self) -> float:
        return _positive(self.policy.get("probe_timeout_s"), DEFAULT_PROBE_TIMEOUT_S)

    # -- probing ---------------------------------------------------------

    def probe(self, ep: Endpoint) -> bool:
        """Send one unauthenticated ``GET <base_url>/models`` and record the result.

        Any HTTP response below 500 counts as reachable, including 401/403:
        the server is up, it just wants credentials PEH does not hold. Network
        errors, timeouts and 5xx put the endpoint on cooldown.
        """
        url = ep.base_url.rstrip("/") + "/models"
        request = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
        status: Optional[int] = None
        error = ""
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.probe_timeout_s) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
        except Exception as exc:  # network error, timeout, bad URL, ...
            error = type(exc).__name__
        latency_ms = (time.monotonic() - started) * 1000.0
        self._record(ep, status, latency_ms, error)
        return bool(ep.healthy)

    def probe_all(self) -> None:
        """Probe every endpoint concurrently, so N dead endpoints cost one timeout, not N."""
        endpoints = list(self.endpoints.values())
        if not endpoints:
            return
        with ThreadPoolExecutor(max_workers=min(MAX_PROBE_WORKERS, len(endpoints))) as pool:
            list(pool.map(self.probe, endpoints))
        logger.info("PEH probed %d endpoint(s)", len(endpoints))

    def _record(self, ep: Endpoint, status: Optional[int], latency_ms: float, error: str) -> None:
        with self._lock:
            ep.last_status = status
            ep.last_error = error
            ep.latency_ms = latency_ms
            if status is not None and status < 500:
                ep.healthy = True
                ep.cooldown_until = 0.0  # recovered: leave cooldown immediately
            else:
                ep.healthy = False
                ep.cooldown_until = time.time() + self.cooldown_s

    # -- ranking ---------------------------------------------------------

    def rank_candidates(self, capability: str = DEFAULT_CAPABILITY) -> List[Endpoint]:
        """Endpoints that support ``capability`` and are not cooling down, best first.

        Order: healthy, then not-yet-probed, then unhealthy; ties by latency, then cost.
        """
        now = time.time()
        with self._lock:
            cands = [
                e for e in self.endpoints.values()
                if capability in (e.capabilities or [DEFAULT_CAPABILITY]) and e.cooldown_until <= now
            ]
            cands.sort(key=lambda e: (_state_rank(e), e.latency_ms, e.cost_per_1k))
        return cands

    def on_failure(self, failed_id: str, capability: str = DEFAULT_CAPABILITY) -> Optional[Endpoint]:
        """Mark ``failed_id`` unhealthy and return the best other candidate, if any.

        Library building block. The plugin does not call this: Hermes offers no
        public hook to report a failed request or to switch providers.
        """
        with self._lock:
            ep = self.endpoints.get(failed_id)
            if ep:
                ep.healthy = False
                ep.cooldown_until = time.time() + self.cooldown_s
        others = [c for c in self.rank_candidates(capability) if c.id != failed_id]
        for cand in others:
            if cand.healthy:
                return cand
        return others[0] if others else None


def _state_rank(ep: Endpoint) -> int:
    if ep.healthy:
        return 0
    if ep.healthy is None:
        return 1
    return 2


def _positive(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default
