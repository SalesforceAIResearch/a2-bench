# a2-bench

An **agent-to-agent (a2a)** benchmark harness, local Docker only. Each run
unit boots one Docker container per role, runs one conversation among the
agents, then evaluates the files they produced. A unit can seat two agents
(an exchange), several (e.g. a dealer working four buyers in a market), or a
single one (a solo baseline: one container, no peer to message — the
intrinsic-behavior control for a multi-agent scenario).

Each role runs one long-lived CLI session. Messages travel host↔container,
never container↔container: each container has its own message hub, and the
host — the only component that sees every hub — drains each role's outbox,
routes every message by its own `to:` field, and delivers it to the
recipient's hub. A recipient's session starts on the first message addressed
to it, and an exited session is resumed when new mail is pending. The host
also re-checks every hop against the task's visibility graph (who may address
whom); an out-of-graph hop is refused and reported to the sender.

The tool surface an agent sees is A2A-shaped — agent cards, an
`[a2a message]` envelope, and four MCP tools:

- `a2a_list_agents` — the directory of agents this role can reach.
- `a2a_send_message` — `to` is an agent name or `"*"` (broadcast). By default
  it waits for the addressee's reply (capped by `timeout_s`, default 240 s; a
  timeout is a note, not an error); `wait_for_reply=false` returns once the
  message is queued, so one agent can contact several peers.
- `a2a_wait_for_messages` — block until mail arrives (optionally only
  `from_agent`), then return everything queued.
- `a2a_task_complete` — registered only for the task's assigned role; closes
  the conversation for everyone.

Nothing in an agent's context frames the setup as an evaluation. The `a2a_`
prefix exists because several harnesses ship native tools under the bare
names for their own agent-team collaboration feature — a bare name would
collide.

## Requirements

- Python >= 3.12, [uv](https://docs.astral.sh/uv/)
- Docker daemon running locally — the sandbox image `a2-bench-ubuntu:latest`
  is auto-built from `docker/ubuntu-base/Dockerfile` on first use;
  coding-agent CLIs are installed per role at run time. Odoo-backed tasks
  (`capability/odoo_write_n2`) use the separate `odoo-erp` image
  (`docker/odoo-erp/`, `FROM a2-bench-ubuntu`) via
  `configs/environments/docker_odoo.yaml`
- `secret/.env` with agent credentials (copy `secret/.env.example`), e.g.
  `OPENAI_API_KEY` for `codex`, `CURSOR_API_KEY` for `cursor`, or
  `ANTHROPIC_API_KEY` / bedrock / vertex / openrouter settings for
  `claude_code`
- Task data — fetched once from a private HuggingFace dataset (next section);
  it is not committed to git
- For the judge-backed interaction metrics only: `uv sync --extra metrics`
  and a judge credential in the same `secret/.env`. A plain run needs neither.

## Task data (HuggingFace)

Task data (the `input/software/reference` files each scenario stages into the
containers) lives in the gated HF dataset `a2a-salesforce/a2-bench`, mirrored 1:1
as `<domain>/<task>/<variant>/[<role>/]{input,software,reference}`.

```bash
export HF_TOKEN=hf_...          # or:  hf auth login

# Fetch everything into ./task-data (re-run to pull updates)
scripts/fetch_task_data.sh
```

`reference/` holds eval answers, so the dataset is private.

## Quick start

```bash
uv sync
scripts/fetch_task_data.sh      # once (see "Task data" above)

# Show the run matrix (task × variant) without executing anything
uv run python -m a2_run run example_exp_a2a.yaml --dry-run

# Run for real
uv run python -m a2_run run example_exp_a2a.yaml

# Useful flags
#   --task PATH   only these tasks    --resume  skip completed units    -v  debug logs
```

Results land under
`.logs/<experiment>/<roles>/<models>/<domain>__<task>/<variant>/<timestamp>/`
(`hops.jsonl` streamed as the conversation runs, `conversation.json` at
finalize, per-role trajectories + origin logs, and eval scores).

## The experiment yaml

`example_exp_a2a.yaml` points at everything else:

- `agents:` — a **role → agent-preset mapping** (one or more roles). It may
  name more roles than any one variant uses: each unit binds only the roles
  its variant declares, so one yaml runs a corpus whose scenarios seat two
  to five agents. A variant declaring a role the yaml does not bind is
  skipped.
- `environment:` — the env yaml (`configs/environments/docker.yaml`).
- `tasks:` — a task-list file under `selected_tasks/` (one `domain/task` per
  line; the runner keeps only variants whose declared roles are covered by
  the binding).
- `secret_file`, `output.root`, `concurrency` (units in flight — each boots
  one container per bound role, so size it to roles × units),
  `wall_time_s` (budget for the whole conversation), `cleanup_mode`
  (`delete | stop | keep`).
- `a2a:` block — `max_messages` (total delivered messages across all
  senders; one message = one hop, default 16 — it replaced `max_rounds`,
  which now fails loudly), `channel` (the delivery envelope — see below).

A run ends on `task_complete`, `max_messages`, `wall_time`,
`invalid_agent_response` (the opener went silent), `all_agents_exited`,
`all_agents_blocked` (every live agent waits on something nothing can
satisfy), or `agent_failed`. A dead or silent non-assigned role does not end
the run: agents waiting on it get a relay notice and the rest continue.

### The `channel` knob

Agents talk over an A2A v1.0 JSON-RPC binding: each role's hub serves
`POST /rpc` (`SendMessage` / `GetTask` / `CancelTask`) plus its own agent card
at `/.well-known/agent-card.json`, and the host is the A2A client on every
side. `channel` selects how much of that envelope the receiving model reads:

- `a2a` (default) — the spec `Message` fields: sender name, `role`,
  `messageId`, `contextId`, `taskId`, then the body.
- `direct` — the body under a bare `from: <name>` line. The pre-protocol
  baseline.

Both arms carry the same body over the same wire, so running a scenario twice
isolates the envelope as a single variable:

```bash
uv run python -m a2_run run example_exp_a2a.yaml -S a2a.channel=direct
uv run python -m a2_run run example_exp_a2a.yaml -S a2a.channel=a2a
```

Note what A2A does *not* define: there is no directory operation
(`a2a_list_agents` is direct-configuration discovery) and no client-side
completion operation (`a2a_task_complete` is the assigned agent declaring its
own task terminal). The MCP tools are a local affordance — how a model asks
its own hub to act — not protocol operations; A2A covers agent-to-agent
communication, MCP covers model-to-tool. Their `a2a_` prefix keeps them from
colliding with a harness's own native agent-team tools of the same bare name.

Agent presets live under `configs/agents/`: `claude_code`, `codex`, and
`cursor` are the real CLI coding agents executed inside the container (incl.
`direct` / `openrouter` / `bedrock` / `vertex` routing for claude_code —
`openrouter` drives *any* OpenRouter model through the same Claude Code
harness, see below; `cursor` talks only to Cursor's backend, with the model
named by a Cursor catalog id such as `cursor-grok-4.6-high`); `dummy`
(`configs/agents/dummy_a2a.yaml`) is a no-LLM scripted responder — pair it
with itself to exercise the whole pipeline token-free.

### CLI overrides — no yaml per combination

Any experiment key can be overridden with repeatable `--set` / `-S KEY=VALUE`
flags, applied to the parsed yaml before validation — one base yaml covers a
whole sweep:

```bash
# swap one role's preset, the task list, and a knob:
uv run python -m a2_run run example_exp_a2a.yaml \
  -S agents.oracle=configs/agents/codex.yaml \
  -S tasks=selected_tasks/a2a_hello.txt \
  -S a2a.max_messages=20 -S output.root=.logs/sweep1

# replace the whole role mapping inline (VALUE is parsed as yaml):
uv run python -m a2_run run example_exp_a2a.yaml \
  -S "agents={shopper: configs/agents/claude_code_vertex.yaml, merchant: configs/agents/codex.yaml}"
```

`KEY` is a dotted path into the yaml; relative paths resolve against the
yaml's directory, exactly as if written in the file. Overrides bypass no
validation. Combine with `--dry-run` to preview the resulting matrix.

### Varying a preset — no config file per model

A role entry can be a mapping instead of a path: `preset:` names the base
yaml and every other key overlays it (`model`, `id`, `executor`, or any
deployer config field such as `effort_level`). So a model variation needs no
new preset file — e.g. Claude Opus 5 via Vertex:

```bash
uv run python -m a2_run run example_exp_a2a.yaml \
  -S "agents.asker={preset: configs/agents/claude_code_vertex.yaml, model: claude-opus-5}" \
  -S "agents.oracle={preset: configs/agents/claude_code_vertex.yaml, model: claude-opus-5}"
```

The dotted form works too, one key at a time:

```bash
  -S agents.asker.preset=configs/agents/claude_code_vertex.yaml \
  -S agents.asker.model=claude-opus-5 \
  -S agents.asker.effort_level=high
```

The same mapping form is valid written directly in the experiment yaml's
`agents:` block. Config fields are still validated against the deployer's
config dataclass, so a typo'd field name fails at load time.

### Non-Anthropic models via OpenRouter — same Claude Code harness

To benchmark GPT, Gemini, Llama, DeepSeek, and the rest **without a new harness**, run the `claude_code` deployer with `provider: openrouter`. OpenRouter exposes an Anthropic-compatible endpoint, so the Claude Code CLI drives any model on it unchanged; the deployer points `ANTHROPIC_BASE_URL` at `https://openrouter.ai/api`, authenticates with a Bearer token, and you name the model with OpenRouter's `vendor/model` ids.

```bash
# 1. Put your OpenRouter key in secret/.env  →  OPENROUTER_API_KEY=sk-or-...
# 2. Point one or both roles at the openrouter preset and pick models:
uv run python -m a2_run run example_exp_a2a.yaml \
  -S "agents.asker={preset: configs/agents/claude_code_openrouter.yaml, model: deepseek/deepseek-v4-flash-0731}" \
  -S "agents.oracle={preset: configs/agents/claude_code_openrouter.yaml, model: deepseek/deepseek-v4-flash-0731}"
```

The key travels through the env passthrough (never the host-gathered `_spec.json`). For adaptive-thinking models over OpenRouter keep `max_thinking_tokens: null` (the preset's default) — see the field docs in `a2_run/agents/claude_code/config.py`. `effort_level` is Anthropic-native and a no-op on non-Claude models, so leave it `null` there.

## Viewing runs

`serve` starts a local, on-demand viewer (no files written; runs re-read on
every request, so an in-progress run shows its latest state on refresh):

```bash
uv run python -m a2_run serve .logs/<experiment>       # a tree of runs
uv run python -m a2_run serve .logs/<...>/<timestamp>  # a single run
uv run python -m a2_run serve .logs --port 9000
```

The index is one sortable, filterable table of runs (filters live in the URL,
so any view is shareable). Clicking a run opens its page: the **transcript**
tab is the merged per-turn timeline (reasoning / tool calls / observations,
then the reply), the **judge** tab holds the interaction metrics with the
judge's evidence quoted and highlighted inline in the transcript, and a score
card shows which rubric criteria earned or lost points.

## Interaction metrics (`metrics/`)

A task's `evaluate()` scores its deliverable. The `metrics/` package scores
the **interaction** instead — per agent, offline, from the recorded run (e.g.
did an agent leak its principal's reservation value, however good the final
paperwork). Pure functions of a finished run dir, so a new metric re-scores
archived runs without re-running them.

```bash
uv sync --extra metrics                              # judge-backed metrics need the SDK
uv run python -m metrics list                        # registered metrics
uv run python -m metrics score .logs/<experiment>    # writes <run_dir>/metrics.json
```

See [`metrics/README.md`](metrics/README.md) for the metric list, the
`Metric` interface, and judge configuration. Scores appear automatically as
columns in the run viewer.

## Tasks

A task directory holds exactly two files, splitting declarative setup from
scoring code:

```
tasks/<domain>/<task>/
  task.yaml    # everything declarative — read on its own, no Python needed
  main.py      # only the scoring code: load()/start()/evaluate() + helpers
```

- **`task.yaml`** declares the roles and how they meet: task identity
  (`domain`/`task`/`variants`), the `vm` block sizing the containers, each
  role's private `roles:` brief + `agent_cards:` (identity/skills), the
  `assignment` for the assigned role, and `paths:` templates the scoring
  code reads. Two optional per-variant blocks are host-side setup only and
  never shown to agents:
  - `visibility:` — who may address whom: `mesh` (default — everyone reaches
    everyone), `star` (the assigned role is the hub; other roles reach only
    it), or an explicit `{role: [peers]}` adjacency map. The graph is made
    symmetric, and an edge naming an unknown role fails loudly.
  - `egress:` — `{role: open | restricted}`. A restricted role can reach only
    its own model backend; the allowlist is derived from the bound
    harness/provider (`a2_run/environments/egress.py`), never written in the
    task.
- **`main.py`** stays thin: `load()` builds the `Task`s from `task.yaml`;
  `start(cfg, sandboxes)` seeds each role's container before the run
  (`sandboxes` is a dict role → `SandboxHandle`, so each role's world can
  differ); `evaluate(cfg, sandboxes)` scores the artifacts afterward, and
  may return a `scored(...)` breakdown (see `tasks/task_spec.py`).

To enable a task, add its `domain/task` line to a file in `selected_tasks/`
and reference that file from the experiment yaml.

## Layout

```
a2_run/            framework package
  cli.py             `python -m a2_run run <spec.yaml>` / `serve <path>`
  orchestration/     config loading, run matrix, unit lifecycle (pair_lifecycle.py,
                     one container per role) + N-agent conversation driver
  protocols/         A2A v1.0: agent_card, message, jsonrpc binding, task_state, channel
  environments/      docker provider, image registry, task-data staging,
                     egress.py (restricted-role backend allowlists)
  base_interface/    SandboxHandle (docker exec/cp), executor + deployer ABCs
  executors/         sandbox (agent runs in container), local (host; dummy agent)
  agents/            claude_code, codex, cursor, dummy deployers
  transcript/        the on-demand run viewer (`serve`)
docker/ubuntu-base/  sandbox image: Dockerfile, a2a_hub.py (PID 1; the role's
                     A2A endpoint), a2a_tools_mcp.py (the local tool surface:
                     a2a_list_agents / a2a_send_message / a2a_wait_for_messages /
                     a2a_task_complete)
docker/odoo-erp/     Odoo 19 + PostgreSQL image (FROM ubuntu-base) for odoo tasks
metrics/           offline interaction metrics (see metrics/README.md)
tasks/             task library (task.yaml declares briefs/vm; main.py = scoring)
task-data/         host-side task data, staged into the containers (fetched)
configs/           agents/ presets, environments/docker.yaml (+ docker_odoo.yaml)
selected_tasks/    plain-text task lists, one `domain/task` per line
example_exp_a2a.yaml  experiment spec: roles + environment + task list + a2a knobs
secret/.env        credentials (gitignored)
```
