"""Load the plugin the way Hermes does (as a package) and exercise register()."""
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def plugin():
    name = "peh_plugin_under_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]


class FakeCtx:
    def __init__(self, settings):
        self.settings = settings
        self.hooks = {}
        self.commands = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, handler, description=""):
        self.commands[name] = (handler, description)


def test_register_wires_hook_and_command(plugin):
    ctx = FakeCtx({"endpoints": [{"id": "a", "base_url": "http://127.0.0.1:1/v1"}]})
    plugin.register(ctx)
    assert "on_session_start" in ctx.hooks
    assert "peh" in ctx.commands
    assert len(plugin._healer.endpoints) == 1


def test_invalid_endpoints_are_skipped_not_fatal(plugin):
    ctx = FakeCtx({"endpoints": [
        {"id": "bad", "base_url": "file:///x"},
        {"id": "good", "base_url": "http://127.0.0.1:1/v1"},
        "junk",
    ]})
    plugin.register(ctx)
    assert list(plugin._healer.endpoints) == ["good"]


def test_peh_command_without_config(plugin):
    ctx = FakeCtx({})
    plugin.register(ctx)
    out = ctx.commands["peh"][0]("")
    assert "no endpoints configured" in out


def test_peh_command_reports_down_endpoint(plugin):
    ctx = FakeCtx({"probe_timeout_s": 1, "endpoints": [{"id": "a", "base_url": "http://127.0.0.1:1/v1"}]})
    plugin.register(ctx)
    out = ctx.commands["peh"][0]("chat")
    assert "a: DOWN" in out
    assert "No endpoint available" in out
    assert "does not switch" in out


def test_session_start_hook_does_not_raise_without_endpoints(plugin):
    ctx = FakeCtx({})
    plugin.register(ctx)
    assert ctx.hooks["on_session_start"](session_id="s") is None
