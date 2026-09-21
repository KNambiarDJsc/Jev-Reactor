# The MCP gateway

`jev-reactor-mcp` is a real [Model Context Protocol](https://modelcontextprotocol.io) server
that sits in front of other MCP servers and decides every `tools/call` before it runs.

```text
MCP client ──tools/call──►  Jev Reactor gateway  ──►  your MCP server(s)
(Claude Desktop,             1. allowlist
 Claude Code, Cursor,        2. argument schema
 your own agent)             3. hard rules (permissions, limits, approval, duplicates)
                             4. Jev's typed judgments, then your policy
                             5. only an allowed call is forwarded
```

A call that is held back **never reaches the downstream server**. The client gets an ordinary
tool result with `isError: true` and a plain reason ("it would repeat information already
retrieved; use the earlier result"), so the model can adapt instead of retrying blindly.

It is a decision gate, not a sandbox. It cannot make an unsafe tool safe; it decides whether
an agent should be allowed to call a tool right now. Jev can still make semantic mistakes, and
nothing here is a guarantee. Read [SECURITY.md](../SECURITY.md).

## Install

```bash
pip install "jev-reactor[mcp]"
```

The MCP dependencies are an optional extra: `pip install jev-reactor` (no extra) does not
import the MCP SDK, and `jev-reactor mcp ...` tells you which extra to install.

## Try it in two minutes (no API key, no network)

```bash
jev-reactor-mcp init --demo -o gateway.yaml   # a config that fronts a safe fake invoice server
jev-reactor-mcp check gateway.yaml            # validates offline; prints what would be exposed
jev-reactor-mcp serve gateway.yaml            # stdio; point any MCP client at this command
```

The demo config uses `provider: {kind: mock}`, a scripted stand-in that approves nearly
everything. It shows the *mechanics* (allowlist, permissions, approval, held-back results). It
says nothing about what Jev would decide. `serve` prints a warning to stderr whenever the mock
provider is in use.

## Use it with a real Jev

```bash
export TYPESAFE_API_KEY=...
jev-reactor-mcp init -o gateway.yaml          # a template: edit downstream, tools, goal
jev-reactor-mcp check gateway.yaml
```

Start with `mode: observe`: every decision is computed and recorded, only your hard rules
stop anything, and you can see what Jev *would* have held back (`would_execute_if_enforced`)
before you let it stop anything. Then move to `guard`, then `enforce`.

## Connect a client

The gateway is an ordinary stdio MCP server, so it plugs in wherever a stdio command does.

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "gated-invoices": {
      "command": "jev-reactor-mcp",
      "args": ["serve", "/absolute/path/to/gateway.yaml"],
      "env": { "TYPESAFE_API_KEY": "..." }
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add gated-invoices -- jev-reactor-mcp serve /absolute/path/to/gateway.yaml
```

**Cursor** (`.cursor/mcp.json`) uses the same `mcpServers` shape as Claude Desktop.

Use the absolute path to the executable if your client does not inherit your shell `PATH`
(for example the `jev-reactor-mcp` inside a virtualenv's `bin/` or `Scripts/`). Give the client
the *gateway*, not the original server, otherwise the agent can simply call the original.

## How a call is decided

For every `tools/call`, in order:

1. **Allowlist.** Only tools named in `tools:` are listed or callable. Everything else the
   downstream server offers is invisible, and calling it returns "not allowed through this
   gateway".
2. **Arguments** are validated against the downstream tool's own JSON Schema. An invalid call
   comes back as a tool error the model can read and fix; nothing is forwarded.
3. **Hard rules, before Jev is asked:** user denials, missing permission, amount over limit,
   irreversible tools needing approval, exact duplicates of a call that already succeeded.
   These never depend on Jev's answer.
4. **Jev's typed judgments** (relevance, redundancy, task completion, injection suspicion, and
   the next step) in one request, then the policy combines them in plain Python.
5. **Forward or hold back.** Only an allowed call is forwarded.

`mode` decides what Jev's verdict may stop:

| Mode | Hard rules | Jev `block` / `review` | Any other non-`allow` action (`skip`, `fallback`, ...) |
|---|---|---|---|
| `observe` | bind (`block`, `review`) | recorded, not enforced | recorded, not enforced |
| `guard` | bind | stops the call | lets it through |
| `enforce` (default) | bind | stops the call | stops the call |

In `observe` an exact duplicate or a redundant read still runs: those are efficiency checks,
not safety checks. Permissions, denials, amount limits and approval for irreversible actions
bind in every mode.

Every result carries the decision in `_meta["io.jev-reactor/decision"]`:
`action`, `reasons`, `executed`, `mode`, `event_id`. The explanation shown to the *model* never
includes thresholds or probabilities, which would help someone tune an injection.

### When Jev is unavailable

The failure mode is decided per risk tier, and nothing defaults to allow: a `write` tool goes
to `review`, an `irreversible` tool is `block`ed (and can never be configured to allow), and a
`read` tool gets the `fallback` action. `fallback` is not `allow`: in `enforce` mode (the
default) the call is **not run**, and in `guard` and `observe` it is. The model is told "the
decision service was unavailable, so the call was not run" when it is held back.

## Approval for irreversible actions

An `irreversible` tool never runs without a human's approval. The gateway asks the human with
MCP **elicitation** through the client, and builds the prompt itself from the real call
(tool, redacted arguments, your goal), never from prose the agent wrote:

- On the **2026-07-28** protocol revision the gateway returns an input-required result with
  request state sealed by the SDK. The answer only counts if it comes back with the state the
  gateway issued *for that exact call*; a bare answer, a tampered state, or a state replayed
  onto different arguments is refused (and the user is asked again).
- On the earlier **handshake** revisions the gateway sends a direct elicitation request.
- If the client **cannot** be asked (no elicitation support), the call is held back with
  "the client could not ask for it". It never runs on the strength of a guess.
- `approvals: {mode: deny}` makes irreversible tools unrunnable through this gateway.

Approval is **never read from tool arguments or `_meta`**. A model that adds `"approved": true`
gains nothing (there is a test for exactly this).

## Configuration reference

```yaml
name: jev-reactor-gateway          # what clients see as the server name
mode: enforce                      # observe | guard | enforce
goal: "What the agent is trying to do"   # or agent_context: ...  (one is required)
provider:
  kind: typesafe                   # or mock (demo only)
  model: null                      # default: jev-latest
deadline_seconds: 1.0              # Jev's answer budget per decision

downstream:                        # one or more MCP servers to front
  invoices:
    command: npx                   # stdio ...
    args: ["-y", "@example/server"]
    env: { API_TOKEN: "${INVOICES_TOKEN}" }   # ${VAR} is read from YOUR environment
    cwd: null
    timeout_seconds: 30            # bounds connecting and every call
  crm:
    url: https://crm.example.com/mcp          # ... or Streamable HTTP
    headers: { Authorization: "Bearer ${CRM_TOKEN}" }

tools:                             # the allowlist; keys are the names clients will see
  search_invoices: { risk: read, idempotent: true, requires_permission: "invoices:read" }
  write_note: write
  send_email: { risk: irreversible, requires_permission: "email:send" }
  refund_payment:
    risk: irreversible
    requires_permission: "payments:refund"
    amount_arg: amount             # a numeric argument, checked against max_amount in code
    max_amount: 500

permissions: ["invoices:read", "email:send"]   # granted by YOU; never by the agent
denied_tools: []
prefix_tool_names: auto            # auto | always | never (auto: "server__tool" if >1 server)
description_mode: truncate         # passthrough | truncate | host (use your own text)
description_max_chars: 400
on_suspicious_description: quarantine   # quarantine | hide | allow
approvals: { mode: elicit }        # elicit | deny
persist: { decisions: decisions.jsonl, state: digest }   # omit to keep nothing
require_session_key: false
history_max: 50
max_sessions: 256
tools_cache_seconds: 30
```

Notes:

- **`risk` defaults to `write`** when you give a bare name: reviewable, never silently allowed
  when Jev is unavailable. Server hints (`destructiveHint`, `readOnlyHint`) can only *raise* a
  risk tier and can never mark a tool idempotent: the server is untrusted.
- **`goal`** is required because MCP `tools/call` carries no goal, and relevance and
  completion questions have nothing to judge without one. It is fixed per gateway; run one
  gateway per task or role.
- **`${VAR}`** references in `env` and `headers` are expanded from your environment. A missing
  variable is an error that names the variable and never prints a value. Secrets do not belong
  in the YAML.
- A stdio child does **not** inherit your environment: it gets a small allow-listed one plus
  what you list under `env`.
- Unknown keys are errors (a typo such as `permisions:` fails loudly instead of granting
  nothing).

## Hostile servers

The downstream server, its tool descriptions and its results are treated as untrusted.

- **Tool descriptions** that read like instructions to the model ("ignore your previous rules",
  `<IMPORTANT>`, "do not tell the user", ...) are withheld from the client (`quarantine`,
  the default), hidden (`hide`), or passed through (`allow`). Long descriptions are truncated,
  and `description_mode: host` replaces them with text you wrote. This is a heuristic filter,
  not a proof; `host` is the strict option.
- **Tool results** are returned unchanged, but the *next* proposed call is judged with the
  recent results in view, so an injected instruction in one result is a signal when the agent
  acts on it.
- **Errors** from a downstream server never reach the model as stack traces or raw messages,
  and never include your config.

## Serving over HTTP

```bash
jev-reactor-mcp serve gateway.yaml --transport http --port 8765
```

- Listens on **loopback only**. A non-loopback `--host` is refused unless you pass
  `--allow-remote`, which in turn requires `--token-env NAME` (a bearer token read from an
  environment variable; never put a token on the command line).
- DNS-rebinding protection is on: a foreign `Host` or `Origin` is rejected.
- **Sessions:** a client may name its conversation with `_meta["io.jev-reactor/session"]`;
  otherwise a handshake-era `Mcp-Session-Id` is used; otherwise all callers share one
  conversation. That is right for one user and wrong for several: set
  `require_session_key: true` for multi-tenant use. The session key scopes *history* (duplicate
  detection, recent calls). It is a convenience, **not authentication**, and permissions are
  global to the gateway. Put real authentication in front of an HTTP gateway.
- The bearer-token check is a single shared secret, not a user system. For OAuth, terminate it
  in a reverse proxy.

In stdio mode, stdout belongs to the protocol: nothing is printed there, and all logging goes
to stderr.

## Persistence and privacy

With `persist.decisions` set, every decision is appended to a JSONL file. By default a record
stores a **digest** of the redacted state, not the state; goal text and payloads are stored
only with `state: redacted`. Secret-shaped values are masked before any write, and before the
approval prompt is built. Redaction is best effort.

The state sent to Jev is processed by TypeSafe: read their Data Processing Agreement first.
**Recorded logs contain Jev output. Do not use them to train or distill a model:** TypeSafe's
Master Customer Agreement (section 2.3(b)) prohibits it.

## What this version does not do

- **Tools only.** Downstream resources, prompts, sampling and server-initiated elicitation are
  not proxied.
- **No `tools/list_changed` push.** The tool table refreshes on a timer (`tools_cache_seconds`)
  and on `tools/list`; a client that never re-lists sees a stale list, but a tool removed from
  the allowlist stops being callable at the next refresh.
- **One goal per gateway**, fixed in config.
- **Approval needs an elicitation-capable client.** Without one, irreversible tools are held
  back, which is the safe outcome, not a silent pass.
- **Not measured against live Jev.** The test suite exercises the protocol, hard rules, policy
  and approval flows with a scripted stand-in; it does not measure how accurate Jev's
  judgments are on your traffic. Calibrate on your own labelled traces
  ([policy-writing.md](policy-writing.md)) before relying on `enforce`.

## Testing your own gateway

The gateway can be driven in memory with the MCP SDK's client, with a real downstream server
and the scripted provider:

```python
from mcp import Client
from jev_reactor.mcp_server.config import parse_gateway_config
from jev_reactor.mcp_server.demo_server import DemoLedger, build_demo_server
from jev_reactor.mcp_server.gateway import build_gateway
from jev_reactor.providers.mock import MockProvider

ledger = DemoLedger()
server = build_gateway(
    config, provider=MockProvider(answers), targets={"invoices": build_demo_server(ledger)}
)
async with Client(server) as client:
    result = await client.call_tool("send_email", {"to": "a@b.c", "body": "hi"})
assert ledger.outbox == []  # a held-back call never reached the server
```

`tests/test_mcp_gateway.py` in the repository is the reference for approval flows, hostile
descriptions, session isolation and the rest.
