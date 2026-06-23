---
name: lobster-authoring
description: How to author a correct .lobster workflow that actually runs on the embedded OpenClaw runner. Read this before writing or editing any pipeline.
---

# Authoring Lobster Workflows (the correct way)

OpenClaw runs `.lobster` pipelines through an **embedded** engine inside the gateway.
The two examples shipped under `skills/taskflow/examples/` contain bugs that do **not**
run on the embedded runner — do not copy them. This is the corrected reference. Always
`lobster run --dry-run <file>` before shipping; it catches most of the mistakes below.

## The one rule that breaks everything: `command:` vs `pipeline:`
- **`command:`** (alias `run:`) → **raw `/bin/sh`**. Use it ONLY for real programs on PATH:
  `node`, `gh`, `gog`, `mcporter`, `summarize`, `curl`, `jq`, `op`, your `.js`/`.sh` scripts.
- **`pipeline:`** → the **lobster DSL**. Use it for DSL verbs: `exec`, `where`, `pick`, `sort`,
  `dedupe`, `group_by`, `template`, `map`, `json`, `head`, `state.get`, `state.set`, `llm_task.invoke`.
- **NEVER** put `exec --json --shell …` or any DSL verb under `command:` — `/bin/sh` runs its
  `exec` builtin and the step dies with `exit 127: exec: --json: not found`. This is the #1 bug.

## Durable state: `state.get` / `state.set` (there is no `fast-io`)
- `fast-io` does not exist on this platform — never call it.
- `pipeline: state.get <key>` reads `$LOBSTER_STATE_DIR/<key>.json` → the value, or `null` if absent.
  `<value> | state.set <key>` writes it. **State persists across separate runs** (counters, queues,
  cursors, dedupe sets). Keys are lowercased and `[^a-z0-9._-]`→`_`, so use stable lowercase keys.
- `LOBSTER_STATE_DIR` must point at the per-agent EFS workspace (the platform sets it); the default
  `$HOME/.lobster/state` is ephemeral and lost on container restart.
- Queue/dedupe: `state.get q` → modify in a `node -e` step → `state.set q`. The `state.*` verbs have
  **no prefix-`list`** — if you need to enumerate keys, keep an explicit index array under one key.

## Tools — use the real ones, skip the fictional ones
- **Real CLIs (under `command:`):** `gog` (Gmail/Calendar/Drive — `gog gmail search … --json`),
  `mcporter call <server>.<tool> --args '<json>' --output text` (Attio + other MCP servers — output
  is **YAML-ish TEXT**, so parse `.stdout`; `$step.json` is `undefined`), `summarize <url|file> --json`,
  `curl` (Apollo/Perplexity etc. via their own API keys), `gh`, `jq`, `node`, `xurl`, `himalaya`.
- **LLM steps:** prefer doing the model judgment in the **agent loop** and passing the result into the
  run via `args`/`stdin`; or use the `pipeline: llm_task.invoke …` DSL verb. Both keep the pipeline
  deterministic.
- **Do NOT call `openclaw.invoke`** from a step (and therefore not `slack`/`cron`/`memory`/`fast-io`
  as tools): the embedded runner has no gateway URL/token, so every such call returns **401**.
- **Fictional — these tools do not exist; do not call them:** `fast-io`, `apollo` (use `curl`),
  `social-intelligence`, `last30days` (use a `date`-windowed search), `taskr`, `local-rag-qdrant`,
  `agent-browser`, `attio-enhanced` (use `mcporter call attio.<tool>`), `business-development`,
  `capability-evolver`.

## References & templating (two grammars — don't mix)
- **Args:** `${name}` — curly braces. **Step refs:** `$id.json.path` — `$` + dotted path, **no braces**.
- **There is no `{{ }}`** — it is left literal. Never use it.
- **Array index is a DOT:** `$x.json.items.0` — **NOT** `items[0]` (which is rejected in conditions and
  left literal in templates).
- **stdin is explicit** — a step gets prior output only if it declares `stdin: $prev.json` (or `.stdout`).
  There is no implicit chaining between steps.
- Pass values into a shell step safely via `env:` (`env: { SEEN: "$seen.json" }`) or the auto-injected
  `$LOBSTER_ARG_<UPPER_NAME>` — don't interpolate untrusted text into the command line.

## Control flow
- **Never `process.exit(1)` to signal a branch** — under default `on_error: stop` it aborts the whole
  run. Scripts must `exit 0` always and carry status in JSON (`{pass:false}`); gate the next step with
  `condition:`.
- **Conditions** (`condition:`/`when:`): `==` `!=` are strict (no coercion: `5 == "5"` is false),
  `< <= > >=` are numeric-only, plus `&& || !` and parens. Gotchas: **`.length` silently evaluates
  false** (have the producing step emit an explicit `count` and gate on `$x.json.count > 0`); word
  operators `and`/`or`/`not` are a parse error (use `&& || !`); a `!`-prefixed condition must be
  quoted (`condition: "!$x.json.skip"`). Condition refs are strict — an unknown step id throws.
- `on_error:` defaults to `stop`; use `continue` or `skip_rest` where a partial failure must not kill
  the run. `retry:` and `timeout_ms:` are available per step.

## Human approval (real gate, not a tool)
- Use a step-level **`approval: required`** (or `{prompt: …}`). The run halts with a `resumeToken`;
  resume with `{action:"resume", token, approve:true}`, and the step exposes `$gate.approved`.
  Put the approval **before** the side effect and gate the side effect on `condition: $gate.approved`.
- Do **not** use `openclaw.invoke --tool lobster --action await-approval` (fictional) or `input:`
  (rejected by the embedded runner).

## Always validate first
`lobster run --dry-run <file>` re-validates the schema, checks every `pipeline:` verb against the
registry, and resolves refs/conditions against placeholders — it catches command-vs-pipeline mistakes,
unknown verbs, bracket-index conditions, and dangling refs before a live run.

See **`reference.lobster`** in this skill for a complete runnable example using only proven mechanisms.
