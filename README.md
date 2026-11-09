# Provider Endpoint Healer (PEH)

A [Hermes](https://hermes-agent.nousresearch.com) plugin that health-checks the model endpoints you list and tells you which one is currently the best candidate. **It reports and recommends. It does not switch providers**: Hermes has no public plugin route for changing the active provider at runtime.

## What it does

- Probes each configured endpoint with `GET <base_url>/models`, concurrently.
- Probes in the background when a session starts (can be turned off), and again every time you run `/peh`.
- Puts unreachable endpoints (network error, timeout, HTTP 5xx) on a cooldown and leaves it as soon as a probe succeeds.
- Ranks endpoints per capability: reachable first, then lowest latency, then lowest cost.
- `/peh [capability]` prints status and the recommendation (default capability: `chat`).

## What it does not do

- No automatic provider swap, no retry of failed requests, no budget or cost enforcement. `cost_per_1k` is only used to break ties when ranking.
- No continuous monitoring: endpoints are probed at session start and on `/peh`, nothing in between.
- No authentication: probes are anonymous. HTTP 401/403 counts as *reachable* (the server answered), not as healthy-and-usable. PEH never reads, stores or sends API keys.
- `Healer.on_failure()` is a library helper for a future Hermes failure/swap hook. The plugin does not call it today.

## Install

```bash
hermes plugins install Alvinagustus/provider-endpoint-healer
hermes plugins enable provider-endpoint-healer
```

## Relationship to fallback_model
PEH does not replace Hermes' built-in `fallback_model`. Use `fallback_model` for automatic
provider failover; PEH provides health status and recommendations.

## Compatibility
- Hermes >= 0.23.0
- Python 3.10+
- No required dependencies (stdlib only).

## Configure

Settings live under `plugins.entries.provider-endpoint-healer.settings` in your Hermes `config.yaml` (see `examples/config.yaml`):

```yaml
plugins:
  entries:
    provider-endpoint-healer:
      settings:
        primary: openrouter:claude-sonnet
        cooldown_s: 60
        probe_timeout_s: 3
        endpoints:
          - id: openrouter:claude-sonnet
            base_url: https://openrouter.ai/api/v1
            capabilities: [chat, code]
            cost_per_1k: 0.003
```

Invalid endpoint entries are skipped with a warning; the rest still load. Config changes apply on the next Hermes start.

## Disclosure

- **Network:** sends anonymous `GET <base_url>/models` requests to the URLs *you* configure, at session start (background thread, bounded by `probe_timeout_s`) and on each `/peh`. No other hosts are contacted. No telemetry.
- **Files:** reads nothing outside Hermes's own plugin settings. Writes nothing.
- **Processes:** no subprocesses, no shell commands, no long-running background process. The session-start probe is a short-lived daemon thread.
- **Credentials:** none used or stored.
- **Dependencies:** standard library only.

## Development

```bash
pip install pytest
pytest
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

Tests use a local HTTP server; no external network needed.

## License

MIT, see `LICENSE`.
