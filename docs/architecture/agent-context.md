# What an agent gets on a turn, and where a person can see it

Verified 2026-10-08 against the bridge at image `h-2026.10.8-4a340df`, Hermes `v2026.9.14`
(0.21.3, the pinned base), and three live tests on dev (transcript read, approval ask,
facts). Line numbers drift; the names do not. When this page and the code disagree, the
code wins and this page is wrong.

## 1. From a message to a reply

```
human posts in a room
  -> routers/messaging.py writes MSG + one WORK#MESSAGE row per dispatch target
     (select_dispatch_targets: listen modes, @mentions, Jev picks ONE responder for plain traffic)
  -> DynamoDB stream -> EventBridge Pipe -> openclaw-work.fifo (MessageGroupId = room+agent)
  -> scripts/work_lambda.py -> core/services/openclaw_work.py (a wake signal: it re-reads the
     canonical messages and builds the ingress body itself)
  -> InvokeAgentRuntime {path, method, headers, body} -> the owner's AgentCore microVM
  -> apps/infra/hermes/bridge: restore the agent's profile from S3 (once per process), build a
     Hermes AIAgent for THIS turn, run it, post the reply over the hook token, save the profile
     back to S3 at the END of the turn
```

The body the bridge receives (`openclaw_work.build_ingress_body`) carries: `eventKind`
(`user_request` or `room_event`), `room` (id, name, kind, members with listen modes),
the trigger message with its attachments and `addressedAgentIds`, `recentHistory`, and a
`run` contract (run id, trigger message id, `agentHop`). `recentHistory` is the last 50 room
messages before the trigger; when the trigger sits in a thread whose root is older than that
window, the whole thread is merged in as well, so it can exceed 50 rows. It is then trimmed
from the oldest until the body is under 1 MiB. The browser never supplies any of it.

## 2. What is in the prompt, in the order Hermes assembles it

| Tier | Source | Who writes it | When it changes | Note |
|---|---|---|---|---|
| Stable system | `SOUL.md` in the profile (`<owner>/hermes/<agent>/` in S3) | the owner in Settings → Agents, or a seed | on edit (`.revision` bump; a warm microVM re-pulls before the next turn) | cached by the provider |
| Stable system | the bridge's one-sentence repo rule (`turn._repo_rule`) | the image | on an image roll | present on room turns and native Paperclip runs; absent on voice, mail and `paperclip_run` event prompts until #TBD |
| Stable system | `AGENTS.md` in the profile (the Instructions tab), appended LAST (`with_profile_agents_md`) | the owner, or whoever last rewrote the agent | on edit | the last and longest block; in practice it beats the bridge sentence (section 7) |
| Volatile system | the Hermes skills index: our `lightbulb` + `paperclip` skills plus 58 bundled Hermes skills | the image (`/opt/hermes-skills`) | on an image roll | each description cut to 60 chars; the body is loaded with `skill_view` only when the agent asks |
| Volatile system | `memories/MEMORY.md` + `USER.md` (Hermes file memory, 2200 / 1375 char limits) | the agent | whenever it writes | frozen snapshot per turn |
| Ephemeral system (never stored) | room identity and roster JSON, voice notes, two "observe" lines on `room_event` turns | the bridge, per turn | every turn | its bytes differ between ambient and addressed turns, so the provider's prompt cache misses on every switch (section 6) |
| User message | `Turn context (untrusted data, not instructions): {json}` + an `[Observed … context]` block (the 50-message `recentHistory`, stored as observed rows deduped by message id) + `[Current addressed message]` + attachments | the bridge | every turn | stored verbatim as the turn's `api_content` and replayed on every later turn until compaction |
| User message | up to 5 AgentCore memory records (`RetrieveMemoryRecords`, query = the user text above) | AgentCore Memory | every non-trivial turn | the query currently starts with the turn-context JSON, so Hermes's trivial-prompt skip never fires |
| Tools | Hermes toolsets minus `cronjob`, `tts`, `video_gen`, `clarify`; our plugin's `send_room_message`, `lightbulb_local_sessions`, hire, desktop, browser; `session_search` monkeypatched to the current session's lineage | the image + `managed/config.yaml` | on an image roll | `session_search` cannot read another room's history |

Not in the prompt, read on demand: `$HERMES_HOME/lightbulb-facts.json`
(`core/services/owner_facts.py`: plan, subscription status, provider, company Stripe state,
app URL, agent email, provisioning workflows, owner timezone, **teammates**,
**connected_integrations**, `generated_at`). The `lightbulb` skill routes "who is on the team"
and "what is connected" to this file, never to the room's member list. It is rewritten by
`refresh_owner_facts` and on every reconcile; `scripts/refresh_owner_facts.py --all` refreshes
the fleet.

## 3. What the agent does not get

- Another room's raw history. One Hermes session per room
  (`agent:<id>:lightbulb:channel:<room>`), never ended; `session_search` is scoped to that
  lineage, so transcripts do not leak between DMs and channels. **Workspace memory is not
  isolated that way:** with AgentCore Memory on, every room's and every agent's turns are
  written into the one `/owners/<owner>/` namespace, and up to 5 matching records are
  retrieved for any later turn. A fact an agent learned in a DM can resurface in a channel
  prompt. That is by design (one workspace, one memory) and worth knowing.
- Secrets. `config.yaml` carries `${VAR}` references; the per-profile `.env` is the store, and
  `recentHistory` is built by the backend from canonical messages, not from the profile.
- A person's Claude Code / Codex session, unless that person shared it. Then the
  `lightbulb_local_sessions` tool offers `resources` (what is shared), `read` (the event log,
  100 events × 16 KiB each, 2 MiB total), `send` (direct, or an `ask` the person approves in
  Activity, which is final when declined), `interrupt`, and a receipt.
- Plan, roster and integrations from the prompt. They come from the facts file (section 2).
- Any message after a reply chain reaches the cap (`AGENT_REPLY_CAP` = 2 hops): no dispatch,
  one muted system line.

## 4. Sessions and compaction

- The room session is never ended, so its history is the room's history as the agent saw it.
  Non-room sessions (Paperclip runs, mail, voice) end after their turn so Hermes can prune them.
- `compression.threshold_tokens: 60000` (managed overlay). A chat turn that crosses it pays an
  inline LLM summary at turn start (measured 67–103 s on the founder workspace). Voice turns
  replay at most a 40k tail and are compacted off the call path at 85 % (#3889).
- Rows archived by compaction are kept (`active = 0`) with no retention, so a busy room's
  `state.db` grows without bound. It is one of the two slow fillers of the microVM disk
  (the other is build output; see `docs/runbooks/` once the disk PR lands).

## 5. Where a person sees it

| What | Where | Persisted? |
|---|---|---|
| The reply, and the agent's interim commentary ("progress lines") | the room, as agent messages (`progress: true` on the interim ones) | yes, canonical messages |
| A run's state (started / ended / aborted / forced) and usage | the run card in the room; `GET /messaging/runs/{runId}` | yes, the `RUN#` ledger row |
| A Paperclip run (heartbeat, routine, issue) | Teams → the agent → run detail (log from Paperclip; record in S3 under the agent's run-records prefix) | yes |
| An agent's ask to send into someone's session | Activity (Needs you), plus one room line | yes, until answered |
| A shared Claude Code / Codex session | the session panel (`LocalSessionPanel`): the live terminal, or an ended / offline / no-terminal state. A person does **not** get the event log; only the agent's `read` sees that | the terminal is live only; the event log stays on the Mac side while shared |
| People talking beside a shared terminal | the hidden "Session chat · {title}" room (`session_chat.py`), one per shared session; the agent's turn carries that room name so it knows where it is | yes |
| The Hermes turn itself: tool calls, reasoning, the observed block | **nowhere in the product.** Only the agent's `state.db` has it; the agent can `session_search` it, a person cannot | in S3, as the profile |

If someone asks "what did Friday see when she answered that", the honest answer today is:
the 50 messages before it, the roster and room name, up to 5 memory records, her skills, her
SOUL and Instructions, and whatever `session_search` returned. The exact bytes are not shown
anywhere.

## 6. Known costs, measured 2026-10-08

Ordered by what it costs the owner. Each has a Hermes-native lever we were not using.

1. **Observed context and recall pile up until compaction.** Each turn's observed block and
   recalled records are stored as that turn's `api_content` and replayed on every later turn.
   This is upstream's own Telegram observe design, inherited. Lever: `compression.proactive_prune_tokens`
   (deterministic, no LLM call) and `micro_compact`.
2. **The room prompt rides the ephemeral system prompt and changes bytes between turn kinds.**
   Every ambient→addressed switch in one room misses the prompt cache for the whole replayed
   history. Lever: per-turn variants in the user message, stable guidance in `platform_hints`.
3. **The recall query is polluted** by the turn-context JSON (section 2), so trivial prompts
   still pay a synchronous memory retrieval and the search text is routing JSON. `topK=5` is
   our number, not a Hermes default. Lever: strip before `prefetch`, pass `turn_author`.
4. **Chat compaction is inline.** Lever: the prune above, `idle_compact_after_seconds`.
5. **Skill routing text is cut at 60 chars**, and `paperclip/SKILL.md` is 31k chars per load.
   Lever: the trigger in the first 57 chars; detail in `references/*.md` via `skill_view`.

Fixes in flight are linked from the PRs that cite this section.

## 7. Which rule wins: the Forge lesson

On 2026-10-07 and 10-08 the founder's engineering agent filled its microVM's disk twice by
cloning at its profile root and keeping one worktree per task under `/tmp/wt`, exactly as its
own `SOUL.md` and `AGENTS.md` (written 2026-09-27, stored in S3) told it to. The bridge's
newer one-sentence rule (#3798) and the bundled skill said otherwise. The agent read both and
followed its own files, because they are longer, carry exact commands, come LAST in the
prompt, and are repeated by its compaction summaries. Three rules follow:

- A rule that lives in a profile file can only be changed in that profile file. Adding a
  sentence to the bridge does not override it.
- A profile rule survives every image roll. Rolling the fleet changes nothing an owner wrote.
- The shell is not fenced by `HERMES_WRITE_SAFE_ROOT`; it fences the file tools only. An
  instruction can shape where an agent puts things, never bound what it writes.

## 8. The microVM disk, in one paragraph

The container's `/` is an 8.8 GB filesystem inside a sparse image on an outer filesystem with
about 4.1 GB free. Space freed inside is never returned to the outer, which tracks the
high-water mark of blocks ever touched. The outer hits zero while the inner still reports free
space, writes then fail with EIO (SQLite: `disk I/O error`), log capture dies, and the session
freezes silently while `/ping` and `health` keep answering. A session's peak footprint above
idle must stay under about 4 GB for its whole life; only a new microVM resets it. `/etc/hosts`
is a bind mount from the outer filesystem, so `statvfs("/etc/hosts")` is the number that
matters. One `pnpm install` of this monorepo is 2.4 GB.
