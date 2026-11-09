import http.server
import threading
import time

import pytest

from src.healer import Endpoint, Healer


# -- local HTTP server -------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    """Yield a function that starts a local server answering with a fixed status."""
    servers = []

    def start(status):
        handler = type("H", (_Handler,), {"status": status})
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}/v1"

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _ep(id, url="http://127.0.0.1:1/v1", **kw):
    return Endpoint(id=id, base_url=url, **kw)


# -- probing -----------------------------------------------------------------

@pytest.mark.parametrize("status,healthy", [(200, True), (401, True), (403, True), (500, False), (503, False)])
def test_probe_status_classification(server, status, healthy):
    ep = _ep("a", server(status))
    h = Healer([ep], {"cooldown_s": 60})
    assert h.probe(ep) is healthy
    assert ep.last_status == status
    assert (ep.cooldown_until > time.time()) is (not healthy)


def test_probe_unreachable_goes_on_cooldown():
    ep = _ep("a", "http://127.0.0.1:1/v1")  # nothing listens on port 1
    h = Healer([ep], {"cooldown_s": 60, "probe_timeout_s": 1})
    assert h.probe(ep) is False
    assert ep.last_status is None and ep.last_error
    assert ep.cooldown_until > time.time()


def test_recovery_clears_cooldown(server):
    ep = _ep("a", server(200))
    ep.healthy = False
    ep.cooldown_until = time.time() + 3600
    h = Healer([ep], {})
    assert h.probe(ep) is True
    assert ep.cooldown_until == 0.0
    assert [e.id for e in h.rank_candidates("chat")] == ["a"]


def test_probe_all_runs_concurrently():
    # Unroutable-by-design local ports fail fast; this checks every endpoint is probed.
    eps = [_ep(str(i), f"http://127.0.0.1:{i + 1}/v1") for i in range(5)]
    h = Healer(eps, {"probe_timeout_s": 1})
    h.probe_all()
    assert all(e.healthy is False for e in eps)


# -- ranking and failure -----------------------------------------------------

def test_rank_prefers_healthy_then_latency_then_cost():
    a = _ep("a", healthy=True, latency_ms=100, cost_per_1k=0.003)
    b = _ep("b", healthy=True, latency_ms=50, cost_per_1k=0.001)
    c = _ep("c", healthy=None)
    d = _ep("d", healthy=False)
    h = Healer([d, c, a, b], {})
    assert [e.id for e in h.rank_candidates("chat")] == ["b", "a", "c", "d"]


def test_rank_filters_capability_and_cooldown():
    a = _ep("a", capabilities=["chat", "code"], healthy=True)
    b = _ep("b", capabilities=["chat"], healthy=True)
    c = _ep("c", capabilities=["code"], healthy=True, cooldown_until=time.time() + 60)
    h = Healer([a, b, c], {})
    assert {e.id for e in h.rank_candidates("code")} == {"a"}
    assert {e.id for e in h.rank_candidates("chat")} == {"a", "b"}


def test_on_failure_swaps_to_best_other():
    a = _ep("a", healthy=True, latency_ms=100, cost_per_1k=0.003)
    b = _ep("b", healthy=True, latency_ms=50, cost_per_1k=0.001)
    h = Healer([a, b], {"primary": "a", "cooldown_s": 60})
    cand = h.on_failure("a", "chat")
    assert cand is not None and cand.id == "b"
    assert a.healthy is False and a.cooldown_until > time.time()


def test_on_failure_no_alternative():
    a = _ep("a", healthy=True)
    assert Healer([a], {}).on_failure("a") is None


# -- config parsing ----------------------------------------------------------

def test_from_dict_ok_and_ignores_unknown_and_runtime_keys():
    ep = Endpoint.from_dict({
        "id": "x", "base_url": "https://h/v1", "capabilities": "chat",
        "cost_per_1k": "0.5", "bogus": 1, "healthy": False, "cooldown_until": 9e18,
    })
    assert ep.capabilities == ["chat"] and ep.cost_per_1k == 0.5
    assert ep.healthy is None and ep.cooldown_until == 0.0


@pytest.mark.parametrize("bad", [
    {"base_url": "https://h/v1"},
    {"id": "x"},
    {"id": "x", "base_url": "file:///etc/passwd"},
    {"id": "x", "base_url": "ftp://h/"},
    {"id": "x", "base_url": "https://h/v1", "cost_per_1k": "abc"},
    "not a mapping",
])
def test_from_dict_rejects_bad_entries(bad):
    with pytest.raises(ValueError):
        Endpoint.from_dict(bad)
