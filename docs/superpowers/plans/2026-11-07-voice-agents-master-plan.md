# Voice Agents — Master Plan

> **For agentic workers:** this is the roadmap. Each phase has (or will get) its own task-level plan in this folder; execute those with superpowers:subagent-driven-development. Do not execute from this file.

**Goal:** Turn Lightbulb into a named voice assistant per business — answers the phone, books and moves appointments, sits in the owner's meetings and does the work that comes out of them — on the super-agent architecture already built, with the bill proportional to use.

**Architecture:** The live conversation is a Gemini Live session driven by the existing LiveKit worker; it reads and does bounded writes through our backend and hands anything open-ended to a Hermes Worker through `start_work`. Workers are Hermes sessions per task or per meeting on a pooled AgentCore runtime (v2), no VM per business. One workspace = one business; Knowledge, Needs you, approvals, memory as built. The website/app is the owner's four surfaces (Home · Needs you · Agent · Settings), voice-first.

**Tech stack:** Python 3.12 FastAPI backend on Fargate (ARM), Temporal Cloud, DynamoDB/S3, Bedrock AgentCore (Runtime v2, Browser, Memory, Identity), Gemini 3.8 Live on Vertex AI via `livekit-plugins-google`, LiveKit Cloud (agents + SIP), Twilio (numbers, SIP, SMS), Recall.ai (Meet bot), Next.js web on Vercel, Expo mobile, Clerk, Stripe + Metronome.

**Spec:** `docs/superpowers/specs/2026-11-07-voice-agents-design.md` (approved 7 Nov 2026). Research: `~/Lightbulb/pivot-research-2026-11-07/` (00–16; `08` verified; `14` the synthesis).

## Global constraints (from the spec; every task inherits them)

- Keep the super-agent architecture and Hermes. Workers are Hermes specialists; only two are seeded (Front desk, Office).
- Nothing runs per business while no call or meeting is happening. No per-business VM, no per-business Secrets Manager secret, no per-business always-on process.
- Dev first; prod only after the founder has seen dev; every prod flip is its own PR with the founder's go at that moment. Never `workflow_dispatch` the deploy workflow without that go.
- Every PR is reviewed before merge (reviewer agent, PASS recorded on the PR); the lead merges only after PASS. Ponytail mode on every implementation (shortest diff, no unrequested abstractions); `/simplify` on every task's diff before review; a phase-gate `/code-review` against the AWS Well-Architected Agentic AI Lens + `/security-review` before any phase reaches prod; `/ponytail-audit` repo-wide after Phase 1's deletions.
- Compliance from the first call: first utterance = "<Name>, <business>'s AI assistant. This call is recorded."; every STT/LLM/TTS vendor on zero-retention; no outbound beyond consented callbacks, confirmations, opt-in reminders; no card capture; Claude never touches PHI (Anthropic BAA = 30-day retention).
- The assistant has its own name chosen by the owner; "Lightbulb" is the product. No chat-platform leftovers in the voice product (no markdown, no composer, no channels).
- Prices: $250/month per business, 1,500 minutes included, $0.35/min overage (changeable).
- Every claim in a PR description is verified or marked unverified; evidence (walk notes, screenshots, metrics) goes to `super-agent-design/evidence/` per phase.

## Review focus (what tests will not catch, by phase gate)

1. **Prod flip with a workspace row `containers.super_agent = False`** — the opt-out field: such a workspace must stay on the legacy path only until the legacy path is deleted, then be migrated explicitly, never silently broken. (Phase 1 task 3 test.)
2. **Old mobile builds (TestFlight 0.4.8 external)** read `features.superAgent` and call `/teams/*`: the live-config constant must be true and `/teams/agents` must keep answering from `agent_registry`. (Phase 1 task 6 test.)
3. **A caller asks for something unchecked on the Agent page** (e.g. a payment): the tool must refuse server-side and Nova must take a message — not refuse in the prompt. (Phase 3 contact-boundary test.)
4. **Recall reconnects mid-meeting**: the bot must not leave on an empty-room timer. (Phase 5 test, replay rig.)
5. **A dormant workspace's runtime must not come back**: no nightly timer, no sweeper, no health probe may wake a per-business session after Phase 2; the only wake is a real call, meeting or owner ask. (Phase 2 test: 24 h of `MemoryUsed-GBHours` ≈ 0 outside live use.)

---

## Phases

| # | Phase | Delivers | Gate to leave it | Plan file |
|---|---|---|---|---|
| 0 | **Stop the bleed** (done 7 Nov; verify) | GooseTown services at 0; dormant prod sessions stopped; nightly CEO opted out for 13 dormant workspaces; dev Paperclip off (pending) | `phase0-prod-cuts.sh check` ≈ 0 GB-h outside live use for 24 h; Activate Founders application sent | `~/Lightbulb/pivot-research-2026-11-07/phase0-prod-cuts.sh` |
| 1 | **Super-agent to prod, legacy out** | prod on the super-agent path; the chat UI kept as the manager surface; Paperclip (code, stack, both Aurora clusters, heartbeats, routines), comms features, sessions/desktop, marketplace, Apps + Stripe Projects, Ideas/Feed/Live UI, OpenSearch gone | founder walks prod; `/ponytail-audit` + `/code-review` (AWS lens) + `/security-review` PASS; bill shows Aurora/Paperclip lines gone | `2026-11-07-phase1-prod-switch-and-legacy-deletion.md` (written) |
| 2 | **Golden-path infra + medical-ready** | pooled AgentCore runtime per tier, sessions per task/meeting on v2; per-owner runtimes, provisioner, boot secrets gone; AgentCore Identity for OAuth; secrets → ~10; memory retention rule; one ARM Fargate task, no NAT, CloudFront free plan; BAAs started (Twilio Security Edition, Google Cloud, LiveKit Scale); zero-retention everywhere; Google calendar-only OAuth client + verification started | 24 h `MemoryUsed-GBHours` ≈ 0 outside live use; Secrets Manager < 20 secrets; Cost Explorer shows the floor ≤ $250/mo; BAA requests filed with dates | to write at phase start |
| 3 | **Phone agent** | Twilio number + SIP → LiveKit (dev project first); Gemini Live session with reads, bounded writes, `start_work`; the contact primitive (phone-number identity, per-contact memory, server-side boundary in `effect_check`); call record (a room per call) with transcript, recording, step-by-step; Front-desk pre-warm per call; adapters: Google Calendar + Square during the call, Nova's calendar + browser write-back after; SMS from Nova's number (10DLC); owner mode in the app (in-app Live re-pointed at the Router) | 50 test calls on dev with the replay scenarios; founder places calls; `/code-review` + `/security-review` PASS | to write at phase start |
| 4 | **Website + mobile** | the four surfaces per spec §5 (Home D with the orb and the timeline; Calls with "what it did"; Needs you cards; Agent page; onboarding; Settings); mobile four tabs; impeccable pass; chat UI deleted once this is live | founder walks dev and says go; impeccable critique PASS | to write at phase start |
| 5 | **Meet agent** (built alongside 3–4; part of the MVP) | one voice on the Recall page (Gemini Live), signed-in bot account (Google Login Groups), tile status, speaks only when named, drafts to chat, guests announced/opt-out/no authority, action items → tasks structurally, Meetings rows + pane, the live-information read surface over every integration, Office specialist pre-warmed per meeting, verbal approvals | the spec's live-ask acceptance list answered in a real meeting; replay rig green | to write at phase start |
| 6 | **Billing + go-to-market** | $250/1,500 min/$0.35 on Metronome; minutes meter + run-out behaviour; salon onboarding end to end; first 5 salons | 5 paying salons live | to write at phase start |
| 7 | **Dental** | NexHealth integration; HIPAA-tier runtime; first practice | 1 practice live under BAA | to write at phase start |

Phases 3, 4 and 5 run in parallel lanes after Phase 2; Phase 1 and Phase 2 are sequential and come first.

## Order inside every phase

1. Write the phase plan (superpowers:writing-plans) from the spec section; the founder reads it.
2. Build task by task with fresh subagents (superpowers:subagent-driven-development): test first, shortest diff (ponytail), `/simplify` on the task's diff, fresh reviewer (spec + standards), PASS posted on the PR, lead merges to main → dev deploys.
3. Walk dev (Chrome / real calls / a real Meet) with evidence in `super-agent-design/evidence/<phase>/`.
4. Phase gate: `/code-review` with `docs/standards/aws-agentic-lens.md` as the standard (written in Phase 1 task 0), `/security-review`, `/ponytail-audit` where the phase deleted code; fix; walk again.
5. Founder sees dev; says go; prod flip PR; prod walk; the phase ledger closes.

## The goal prompt

The standalone prompt for the autonomous run is `docs/superpowers/plans/2026-11-07-voice-agents-goal-prompt.md`. It names the goal, the authority and its limits, the order, the gates, and when to stop and ask.

## Open items carried from research

- Test `gpt-realtime-2.1-mini` over LiveKit SIP and Gemini session resumption in the plugin on day one of Phase 3.
- Whether a paid Twilio number can be a consumer-Google-Voice forward target (unverified) — test before telling GV users to port.
- Twilio ISV 10DLC path and lead time (Phase 3 plan).
- AgentCore v2 cold start (unpublished) — measure in Phase 2.
- The CLI's future (becomes the GooseTown CLI) is coordinated with the GooseTown agent; its routes stay until then.
