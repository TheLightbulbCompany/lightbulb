# PostHog person timeline: three small fixes (review copy)

Disposable plan, deleted when the PR ships. Prompted by the 2026-09-30 Shafil
signup trace. Goal, in the owner's words: PostHog should "tell us" what a new
user did, and Gauge should be able to read it off the person's timeline.

Scope: five changes, one PR. Nothing else (no alarms, tables, sweepers,
admin sections, new metrics). The growth-loop session owns the product side
(nudges, invites, in-app-browser handling).

Strike anything below you disagree with.

---

## 1. Trial events land on the person, not on an `org_…` ghost

**Problem.** Five server events are captured with the org id as the PostHog
person. They land on a person named `org_3K1u…` that nobody opens. On Shafil's
own timeline (Clerk user `user_3K1u…`) there is no `trial_started`.

| event | file | today | after |
|---|---|---|---|
| `trial_start_requested`, `trial_started`, `trial_start_failed` | `core/services/user_provision.py:77-97` | person = org id | person = the human who created the org, or who hit the trial-default door |
| `trial_cap_reached` | `core/services/chat_gate.py:140` | person = org id | person = the human whose send was capped (`user_id` is already a parameter of `gate_chat`) |
| `trial_expired_block` | `core/services/provision_gate.py:353` | person = org id | person = the human polling status, when the caller has one |
| `trial_first_agent_response` | `core/observability/product_analytics.py:202`, scheduled from `core/services/messaging.py:3425` | person = org id | person = the human whose message the agent answered |

Every one keeps `workspace_id = <org id>` as a property, the shape the
messaging events (`room_message_sent`, `agent_reply_delivered`) already use.

**Mechanics.**

- `product_analytics.capture_once_per_owner_per_day(owner_id, event, properties, distinct_id=None)`:
  new optional `distinct_id`; falls back to `owner_id`. Dedup key stays
  `(event, owner, day)`. Adds `workspace_id` to the properties.
- `product_analytics.schedule_trial_first_agent_response(owner_id, distinct_id=None)`:
  same fallback. The once-ever billing CAS is unchanged.
- `user_provision._ensure_trial_row(workspace_id, actor_user_id)`:
  `ensure_workspace_trial_default` passes `auth.user_id`;
  `provision_new_workspace(org_id, created_by)` passes the webhook's
  `data["created_by"]` (`routers/webhooks.py:356`, already read there).
- `chat_gate.gate_chat`: pass its existing `user_id` through.
- `provision_gate.evaluate_provision_gate(owner_id, is_admin, actor_user_id=None)`:
  `routers/container.py:86,126` and `routers/teams/agents.py:377` pass
  `auth.user_id`. The three callers with no human in hand
  (`container_recover.py:82`, `user_provision.py:111,170`) pass nothing and
  keep the org fallback. The deterministic `trial_started` uuid stays keyed on
  `workspace:day` so a Svix redelivery still dedupes.
- `messaging._emit_reply_telemetry`: pass `(row.get("author") or {}).get("id")`
  when a human message was matched.

**Trade-off.** The month of org-keyed persons already in PostHog stays as is.
No backfill. The growth funnel (`ops/growth.py`, `uniq(distinct_id)` per
stage) starts counting the same kind of person at every stage from the deploy
onward.

**Tests.** `tests/unit/services/test_user_provision.py`,
`test_chat_gate.py`, `test_provision_gate.py`, `test_messaging_telemetry.py`,
`tests/unit/observability/test_product_analytics*.py` assert the new person
and the `workspace_id` property. No new test files.

---

## 2. The stepper stops reporting a phantom second onboarding

**Problem.** `onboarding_step_entered` fires on every `/chat` mount, including
a reload or a returning user, with a transient `payment` phase before billing
loads (`ProvisioningStepper.tsx:461,523-546`). Shafil's timeline therefore shows
`payment → container → ready` twice, one minute apart. Gauge reported that as a
possible "reload or persistence loop". It is noise.

**Change.** One guard. The capture at `ProvisioningStepper.tsx:541` runs only
when `inSignupFlow` is true, the flag computed six lines above it for the same
purpose (`signup_exit` already uses it).

```tsx
if (inSignupFlow) {
  capture("onboarding_step_entered", { ...unchanged });
}
```

`onboarding_ready`, `onboarding_step_completed` and `onboarding_gate_blocked`
are untouched.

**Test.** One case in `components/chat/__tests__/ProvisioningStepper.test.tsx`:
a mount with the recovery trigger, or an already-ready workspace, captures no
`onboarding_step_entered`.

---

## 3. One client event when a person opens a room

**Problem.** The only evidence that Shafil saw the welcome message is a
`READ#` cursor row in DynamoDB. PostHog has nothing between
`onboarding_ready` and the first `room_message_sent`, so "looked and left"
is indistinguishable from "never looked". Autocapture is off on `/chat` on
purpose, and selecting a room does not change the URL, so `$pageview` does
not cover it.

**Change.** `room_opened` with two properties: `room_kind`
(`channel` / `dm` / `group-dm`) and `workspace_id`. No room name, no room id:
room names can be customer content.

Fired from an effect on the active room in
`components/messaging/MessagingWorkspaceContext.tsx`, so both an explicit
pick and the fresh-workspace auto-open of `#general` (which bypasses
`chooseRoom`) count. `"room_opened"` is added to the `AnalyticsEvent` union in
`lib/analytics.ts`.

**Test.** One case: changing the active room captures `room_opened` with the
room's kind.

---

## 4. One client event when a person starts typing

**Problem.** Nothing distinguishes "looked and left" from "started to type and
gave up". For a silent signup that is the whole question, and no source today
answers it.

**Change.** `composer_typing_started` with `room_kind` and `workspace_id`, no
content, at most once per room per mount. Fired in
`MessagingWorkspaceContext.handleTyping` on the branch that already flips
`typingActive` from false to true (line ~356), guarded by a `useRef<Set<string>>`
of room ids already reported. The 3 s typing timer is untouched; it governs
the indicator, not this event. `"composer_typing_started"` joins the
`AnalyticsEvent` union.

**Test.** One case: two typing bursts in one room capture one event; a
second room captures a second.

---

## 5. Session replay at 100% while signups are this few

**Problem.** `PostHogProvider.tsx:154` samples replay at 0.25. At the current
signup volume that is one or two recorded sessions out of every seven, and
Shafil's was not one of them. A recording would have answered "why did he
leave" in a two-minute watch instead of a six-table reconstruction.

**Change.** `sampleRate: 1` with a comment carrying the date and the reason,
and a note to drop it back when volume justifies. Masking rules are
unchanged: password and email inputs masked, `[data-private]` blocked, and
autocapture stays off on `/chat`. Cost: rrweb runs on `/chat` for every
session; the existing comment in the file describes that cost and it is
accepted for now.

**Test.** `components/__tests__/PostHogProvider.test.tsx` pins the init
options; update the expected `sampleRate`.

---

## Verification in dev, after merge

Fresh test workspace on dev.flickmeon.ai:

1. Sign up, let it provision. On the user's PostHog person: `trial_started`
   with `workspace_id`, and no new `org_…` person.
2. `room_opened {room_kind: "channel"}` from the auto-opened `#general`.
3. Reload `/chat`: no new `onboarding_step_entered`.
4. Type a few characters without sending: one `composer_typing_started`.
5. The session shows up under Session replay with the recording playable.

## Not in scope, parked

Provision success metric and alarm P15 (two lines, reliability truthfulness,
owner said alarms are not the point). The registered-but-never-firing client
events (`chat_message_sent` and friends). Mobile emits no PostHog at all.
Re-keying `automation_armed`, `catalog_agent_deployed`,
`agent_run_completed`, which are also org-keyed but not on the onboarding path.
