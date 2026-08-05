# Architecture and Hermes Compatibility

## Phase 0 result

Hermes Fleet targets the current installed Hermes source at `/home/kyle/.hermes/hermes-agent`, inspected on clean `main` at `a991dfc25` on 2026-08-05. The separate checkout at `/home/kyle/Create/repos/hermes-agent/hermes-agent-projects` is on `feat/projects-ability`, contains unrelated dirty work, and is not used as the API authority or modified by Fleet.

## General plugin API

Current directory plugins require `plugin.yaml` and `__init__.py` with `register(ctx)`.

Fleet can use these public methods from `hermes_cli.plugins.PluginContext`:

- `register_tool(name, toolset, schema, handler, check_fn=None, requires_env=None, is_async=False, description="", emoji="", override=False)`
- `register_cli_command(name, help, setup_fn, handler_fn=None, description="")`
- `register_command(name, handler, description="", args_hint="")`
- `register_skill(name, path, description="")`

Tool schemas passed to `register_tool` expose top-level `name`, `description`, and `parameters`; the registry wraps them for model providers. Fleet tool handlers return JSON strings. Fleet's network-bound tool handlers register with `is_async=True`.

A standalone/user plugin remains opt-in through `plugins.enabled`. A code change requires a Hermes process or gateway restart; a new session/reset is then required for the model tool inventory.

## Native A2A API

Hermes ships `plugins/platforms/a2a`, a stdlib A2A v1.0 server and outbound tool implementation.

- Discovery: `GET /.well-known/agent-card.json`; legacy `/.well-known/agent.json` also answers.
- JSON-RPC: `POST` to the JSONRPC interface URL advertised in `supportedInterfaces`.
- Canonical send method: `SendMessage`; legacy `message/send` remains accepted.
- Request Message fields: `role: ROLE_USER`, `parts: [{text, mediaType: text/plain}]`, `messageId`, optional `contextId`.
- Canonical response result wraps either `task` or `message`.
- Task fields include `id`, `contextId`, `status.state`, optional `status.message`, and optional `artifacts`.
- Terminal states use `TASK_STATE_*` values.
- Per-peer inbound auth is configured with `A2A_PEER_TOKENS=name:token,...`; trusted identities use `A2A_TRUSTED_PEERS` or `a2a.trusted_peers`.
- No tokens means localhost-only. Remote bind widening requires both a token and explicit `A2A_HOST`.
- Server-side reply timeout is `A2A_REPLY_TIMEOUT`; `GetTask`/subscriptions exist, but Fleet v0.1 remains synchronous.

## Transport decision

Fleet does **not** import Hermes' internal `plugins.platforms.a2a.tools` module. That helper is synchronous, accepts arbitrary model-supplied URLs, reads inline token values from `config.yaml`, and returns presentation strings rather than Fleet's structured result models.

Fleet implements one minimal async `httpx` transport behind `FleetTransport`:

```python
class FleetTransport(Protocol):
    async def discover(self, node: NodeConfig) -> NodeSnapshot: ...
    async def send_task(
        self,
        node: NodeConfig,
        task: str,
        *,
        timeout_seconds: int,
        context_id: str | None = None,
    ) -> NodeTaskResult: ...
```

There is no runtime fallback to the Hermes helper, official SDK, or a second protocol. A2A version tolerance is confined to card and response parsing inside `hermes_fleet/transport/a2a.py`.

## Verification run

Executed against the current Hermes source:

```text
env PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest -p no:cacheprovider \
  tests/plugins/test_a2a_plugin.py \
  tests/plugins/test_a2a_phase23.py \
  tests/hermes_cli/test_plugin_cli_registration.py \
  tests/test_plugin_skills.py -q

176 passed, 17 deselected in 3.49s
```

This verifies the live A2A server/client protocol behaviors and public plugin CLI/skill registration contracts used by Fleet.
