"use client";

/**
 * The "when an app does something" trigger flow, shared by BOTH doors into it:
 * the New-automation dialog (RoutinesPanel) and Add-trigger on an automation
 * that already exists (RoutineDetailPanel).
 *
 * A routine is instructions + an owner agent; a trigger is a schedule or a
 * webhook, and a routine can have many. Which door you came in should never
 * decide which triggers you can add — so the app/event picker, the
 * provider-side wiring (auto-register for GitHub/Vercel/Meta/Google,
 * guided paste for Stripe/PostHog/Sentry/Linear/Attio) and the finish dialog
 * live here once, and both surfaces mount the same thing.
 *
 * Server contract: POST /automations/from-integration
 * (routers/automations.py -> core/services/inbound_triggers.seed_trigger_routine).
 * With `routine_id` it ATTACHES to an existing automation instead of seeding a
 * new one.
 */

import { useEffect, useRef, useState } from "react";
import { Blocks, Cable, Check, ChevronDown, Clock } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { EditorialButton, EditorialDialog, EditorialSelect } from "@/components/teams/shell";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { AutomationGoogleAccount } from "@/components/teams/panels/AutomationGoogleAccount";
import { SecretField } from "@/components/teams/shared/components/SecretField";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { useConnections } from "@/hooks/useConnections";
import { useApi } from "@/lib/api";
import { notifyConversionGate } from "@/lib/notify";
import { cn } from "@/lib/utils";

/** Human copy for a backend error: a blocked-gate 402 puts its copy in
 * `detail.message`, while a plain `HTTPException(400, detail="…")` — every
 * caller-facing 400 this flow raises, e.g. "add them as separate triggers" —
 * makes `detail` the string itself. Reading only the former rendered those as
 * "ApiError: API 400", which tells the owner nothing about what to fix. */
export function gateMessage(e: unknown): string {
  const detail = (e as { body?: { detail?: unknown } })?.body?.detail;
  if (typeof detail === "string" && detail) return detail;
  const msg = (detail as { message?: string } | undefined)?.message;
  return typeof msg === "string" && msg ? msg : String(e);
}

/* ── what starts this? ──────────────────────────────────────────────────── */

// The three ways a routine can be kicked off today. (Email intake and
// agent-watch triggers are on the roadmap but deliberately not surfaced here —
// a picker padded with disabled "coming soon" options is clutter, not a
// feature; they'll appear when they actually work.)
export type StartRung = "connected_app" | "webhook" | "schedule";

export const START_RUNGS: ReadonlyArray<{
  value: StartRung;
  label: string;
  hint: string;
  icon: LucideIcon;
}> = [
  {
    value: "connected_app",
    label: "A connected app",
    hint: "When something happens in a connected app like Stripe.",
    icon: Blocks,
  },
  {
    value: "webhook",
    label: "Any other app",
    hint: "Give any service a signed URL to call when its event fires.",
    icon: Cable,
  },
  {
    value: "schedule",
    label: "On a schedule",
    hint: "Run automatically on a repeating cadence — hourly, daily, weekly.",
    icon: Clock,
  },
];

/** The rung radiogroup (WAI-ARIA APG roving tabindex): only the checked radio
 * is a tab stop; Arrow keys move both the selection and DOM focus, wrapping at
 * the ends. */
export function StartRungPicker({
  value,
  onChange,
  label = "What starts this automation?",
  heading = "What starts this?",
}: {
  value: StartRung;
  onChange: (next: StartRung) => void;
  label?: string;
  heading?: string;
}) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);

  function move(delta: 1 | -1) {
    const i = START_RUNGS.findIndex((r) => r.value === value);
    const next = (i + delta + START_RUNGS.length) % START_RUNGS.length;
    onChange(START_RUNGS[next].value);
    refs.current[next]?.focus();
  }

  return (
    <div className="block">
      <span className="label-caps text-[var(--midnight-muted)]">
        {heading}
      </span>
      <div
        role="radiogroup"
        aria-label={label}
        className="mt-2 flex flex-col gap-1.5"
        onKeyDown={(e) => {
          if (e.key === "ArrowRight" || e.key === "ArrowDown") {
            e.preventDefault();
            move(1);
          } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
            e.preventDefault();
            move(-1);
          }
        }}
      >
        {START_RUNGS.map((r, i) => {
          const Icon = r.icon;
          const active = value === r.value;
          return (
            <button
              key={r.value}
              ref={(el) => {
                refs.current[i] = el;
              }}
              type="button"
              role="radio"
              aria-checked={active}
              tabIndex={active ? 0 : -1}
              onClick={() => onChange(r.value)}
              className={cn(
                "flex items-start gap-3 rounded-md border px-3 py-2.5 text-left transition-colors",
                active
                  ? "border-[var(--midnight)] bg-[var(--cream-deeper)]"
                  : "border-[var(--edge)] hover:bg-[var(--cream-deep)]",
              )}
            >
              <span
                className="mt-px flex h-7 w-7 shrink-0 items-center justify-center rounded-md"
                style={{
                  background: active
                    ? "color-mix(in srgb, var(--midnight) 12%, transparent)"
                    : "var(--cream-deep)",
                  color: active ? "var(--midnight)" : "var(--midnight-muted)",
                }}
              >
                <Icon size={16} strokeWidth={1.75} />
              </span>
              <span className="min-w-0 flex-1">
                <span
                  className={cn(
                    "block text-sm font-medium",
                    active ? "text-[var(--midnight)]" : "text-[var(--midnight-soft)]",
                  )}
                >
                  {r.label}
                </span>
                <span className="block text-xs leading-snug text-[var(--midnight-muted)]">{r.hint}</span>
              </span>
              {active && (
                <Check size={16} strokeWidth={2} className="mt-px shrink-0 text-[var(--midnight)]" />
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* ── the connected-app flow ─────────────────────────────────────────────── */

// What the auto-registering shapes' third dropdown picks — the provider-side
// resource the backend hooks ("owner/repo" for GitHub, a project for Vercel,
// a connected Google account email for Google watch channels).
// The label heads the dropdown; the plural feeds its placeholder copy.
// Keyed by registration shape, with an optional "<shape>:<provider>" override:
// auto_meta is the one shape two providers share, and its resource is a Page
// for Meta Ads but an account for Instagram.
const AUTO_RESOURCE_NOUNS: Record<string, { label: string; plural: string }> = {
  auto_github: { label: "Repository", plural: "repositories" },
  auto_vercel: { label: "Project", plural: "projects" },
  auto_google_watch: { label: "Google account", plural: "accounts" },
  "auto_meta:meta_ads": { label: "Page", plural: "Pages" },
  "auto_meta:instagram": { label: "Instagram account", plural: "accounts" },
};

// Shapes whose setup WRITES THE AUTOMATION'S OWN INSTRUCTIONS. Attaching one
// to an automation somebody wrote by hand appends a delimited, platform-owned
// block at the END of its instructions (core/services/inbound_triggers.py::
// _append_platform_block) — nothing they wrote is overwritten. That is a real
// change to their automation, so it is stated BEFORE they confirm rather than
// discovered afterwards.
const ATTACH_DISCLOSURES: Record<string, string> = {
  auto_google_watch:
    "Google's push channel expires within 7 days, so this adds two things to this automation: a short renewal step appended to the END of its instructions, and a twice-weekly schedule trigger that runs it. Everything you wrote stays exactly as it is, above the added block.",
  owner_setup:
    "Gmail's push watch expires within 7 days, so this adds two things to this automation: a short renewal step appended to the END of its instructions, and a twice-weekly schedule trigger that runs it. Everything you wrote stays exactly as it is, above the added block.",
  poll: "This event has no webhook — it checks the inbox on a schedule. So this adds a trigger that runs every 10 minutes, and appends the check instructions to the END of this automation's instructions. Everything you wrote stays exactly as it is, above the added block.",
};

// POST /automations/from-integration's response (core/services/
// inbound_triggers.py::seed_trigger_routine). `guided` -> the owner must
// paste the proxy URL into the provider's own dashboard and then paste back
// the signing secret it hands them. `secret_hint` names the provider's term
// for that secret ("whsec_…" for Stripe, "Client Secret" for Sentry) and
// becomes the paste input's placeholder. `webhook_secret` -> the REVERSE-
// guided variant (PostHog): the secret flows the other way — the owner sets
// it as the provider-side Authorization header, shown once here with no
// paste-back step, so Done is never gated on it. `resource` echoes the
// auto-registered target ("owner/repo" for GitHub, the project id for
// Vercel) so the success copy can name it. `poll` -> a schedule-backed
// automation (gmail.poll): no webhook exists, so trigger_public_id/proxy_url
// are null. Non-guided results may still carry `instructions`
// (gmail.message's one-time Pub/Sub setup) — informational only, Done is
// never gated on them.
export interface FromIntegrationResult {
  routine_id: string;
  trigger_public_id: string | null;
  proxy_url: string | null;
  guided: boolean;
  instructions?: string | null;
  secret_hint?: string | null;
  webhook_secret?: string | null;
  resource?: string | null;
  poll?: boolean;
}

// The registry template's placeholders, rendered client-side exactly as the
// seed renders them server-side (str.format with event_label/account) so the
// pre-filled draft IS what the server would have written.
function fillTemplate(text: string, vars: { event_label: string; account: string }): string {
  return text
    .replaceAll("{event_label}", vars.event_label)
    .replaceAll("{account}", vars.account);
}

/* ── multi-event drafting ────────────────────────────────────────────────
 *
 * ONE trigger carries MANY events: a Paperclip webhook trigger has no event
 * filter, and Stripe/GitHub/Vercel/Meta all let one endpoint subscribe to
 * many events — so ticking three Stripe events is one URL to paste, not
 * three. Everything below mirrors inbound_triggers.py so the draft shown here
 * is byte-for-byte what the server writes when it is left untouched.
 */

/** `_join_human`: one item is itself (which is what keeps every single-event
 * draft identical to the pre-multi-select one), two join with "and", more
 * comma-separate with a final "and". */
function joinHuman(parts: string[]): string {
  if (parts.length < 3) return parts.join(" and ");
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

// inbound_triggers._MULTI_EVENT_CLAUSE, verbatim.
function multiEventClause(count: number, eventLabels: string): string {
  return (
    `This trigger is armed for ${count} events: ${eventLabels}. Exactly one of ` +
    "them fired this run — read the event type out of the payload FIRST and " +
    "branch on it before doing anything else."
  );
}

// Paperclip caps routine titles at 200 chars (inbound_triggers._ROUTINE_TITLE_MAX);
// a joined multi-event title reaches that, and the server falls back to the
// same compact name, so the draft has to as well.
const ROUTINE_TITLE_MAX = 200;

/**
 * All the state and wiring for "a connected app fires this". `routineId` names
 * an existing automation to ATTACH to (null = seed a new one), and is read at
 * submit time so a picker above can change it freely.
 */
export function useConnectedAppTrigger({
  routineId,
  onProviderChange,
}: {
  routineId: string | null;
  /** Fired when the app is switched, so a host owning state that depends on
   * it (the create dialog's attach target) can reset alongside. */
  onProviderChange?: () => void;
}) {
  const api = useApi();
  const { connections } = useConnections();
  const [provider, setProvider] = useState<string | null>(null);
  // The events this ONE trigger is armed for, in the order they were ticked —
  // the first is the primary one (it picks the registration shape, exactly as
  // the seed does server-side).
  const [eventIds, setEventIds] = useState<string[]>([]);
  const [result, setResult] = useState<FromIntegrationResult | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Auto-register state — the provider-side resource (repo/project) to hook,
  // and the picker feed fetched lazily once such a provider is selected.
  // resourceOptions null = not loaded yet (also what re-arms the fetch effect).
  const [resource, setResource] = useState<string | null>(null);
  const [resourceOptions, setResourceOptions] = useState<{ id: string; label: string }[] | null>(
    null,
  );
  const [resourceError, setResourceError] = useState<string | null>(null);
  // owner_setup state (gmail.message) — the owner's own Pub/Sub topic.
  const [topic, setTopic] = useState("");
  // The owner's edits to the pre-filled draft. `null` = untouched, which is
  // what keeps the 1-click path byte-identical: an untouched field sends NO
  // override, so the registry template still renders server-side. It also
  // makes the draft re-derive itself for free when the app/event/account
  // changes — there is no stale copy to resync.
  const [titleEdit, setTitleEdit] = useState<string | null>(null);
  const [descriptionEdit, setDescriptionEdit] = useState<string | null>(null);
  // Guided paste-back state.
  const [whsecInput, setWhsecInput] = useState("");
  const [whsecSubmitting, setWhsecSubmitting] = useState(false);
  const [whsecSaved, setWhsecSaved] = useState(false);

  // Trigger-capable = connected AND the registry declares inbound events for
  // it (mirrors the Integrations card's `can_trigger` gate).
  const triggerableConnections = connections.filter(
    (c) => c.status === "connected" && (c.inbound?.events.length ?? 0) > 0,
  );
  const selectedConnection = triggerableConnections.find((c) => c.provider === provider) ?? null;
  const events = selectedConnection?.inbound?.events ?? [];
  // Ticked order, so the primary event (which drives the registration shape
  // and the starter-prompt override) is the one the owner chose first.
  const selectedEvents = eventIds
    .map((id) => events.find((e) => e.id === id))
    .filter((e): e is NonNullable<typeof e> => e != null);
  const selectedEvent = selectedEvents[0] ?? null;
  // Can this provider's events share ONE trigger? Only if a delivery's body
  // says which one fired — the agent is handed the payload and nothing else
  // (Isol8AI/isol8#1916). GitHub and Sentry name it in a header, so their
  // picker stays single-select and the seed would 400 a multi-event body.
  //
  // This also makes a registration-shape conflict unreachable from the UI:
  // the only provider whose events differ in shape is google (gmail.poll /
  // gmail.message vs the watch events), and google is single-select here. The
  // seed's own conflict 400 remains the fence for direct API callers.
  const multiSelect = selectedConnection?.inbound?.event_in_body === true;
  const registrationShape = selectedConnection?.inbound?.webhook_registration ?? "";
  const isAutoRegister = registrationShape.startsWith("auto_");
  // The selected EVENT's registration override wins, same precedence the
  // backend applies ("owner_setup" adds the Pub/Sub topic input).
  const effectiveRegistration = selectedEvent?.registration ?? registrationShape;
  const isOwnerSetup = effectiveRegistration === "owner_setup";
  const resourceNoun = AUTO_RESOURCE_NOUNS[`${registrationShape}:${provider}`] ??
    AUTO_RESOURCE_NOUNS[registrationShape] ?? { label: "Resource", plural: "resources" };
  // What attaching this shape to a hand-written automation will add to it,
  // stated before the owner confirms. Empty for the shapes that only ever add
  // a trigger.
  const attachDisclosure =
    routineId != null ? (ATTACH_DISCLOSURES[effectiveRegistration] ?? null) : null;

  // The draft: exactly what the server would write if we sent no override.
  // One event joins to itself, so a single selection renders byte-for-byte
  // what it always did; several add the branch-on-the-payload clause.
  const template = selectedConnection?.inbound?.template ?? null;
  const eventLabelText = joinHuman(selectedEvents.map((e) => e.label));
  const compactName =
    selectedEvents.length > 1
      ? `${selectedConnection?.display_name ?? provider} (${selectedEvents.length} events)`
      : (selectedConnection?.display_name ?? provider ?? "");
  const templatedTitle =
    template && selectedEvent
      ? fillTemplate(template.title, { event_label: eventLabelText, account: resource ?? "" })
      : "";
  const draftTitle =
    selectedEvents.length > 1 && templatedTitle.length > ROUTINE_TITLE_MAX
      ? compactName
      : templatedTitle;
  const templatedDescription =
    template && selectedEvent
      ? fillTemplate(selectedEvent.starter_prompt || template.starter_prompt, {
          event_label: eventLabelText,
          account: resource ?? "",
        })
      : "";
  const draftDescription =
    templatedDescription && selectedEvents.length > 1
      ? `${templatedDescription}\n\n${multiEventClause(selectedEvents.length, eventLabelText)}`
      : templatedDescription;
  const title = titleEdit ?? draftTitle;
  const description = descriptionEdit ?? draftDescription;

  useEffect(() => {
    if (!isAutoRegister || !provider || resourceOptions !== null || resourceError !== null) return;
    let cancelled = false;
    (async () => {
      try {
        const resources = (await api.get(`/automations/providers/${provider}/resources`)) as {
          id: string;
          label: string;
        }[];
        if (!cancelled) setResourceOptions(resources);
      } catch (e) {
        if (!cancelled) setResourceError(gateMessage(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isAutoRegister, provider, resourceOptions, resourceError, api]);

  const canSubmit =
    provider != null &&
    eventIds.length > 0 &&
    (!isAutoRegister || resource != null) &&
    (!isOwnerSetup || topic.trim() !== "");

  /** Tick / untick one event. The draft re-derives itself for free — a field
   * the owner has typed into holds its own edit (titleEdit/descriptionEdit),
   * an untouched one follows the selection. */
  function toggleEvent(id: string) {
    setEventIds((prev) =>
      !multiSelect
        ? [id]
        : prev.includes(id)
          ? prev.filter((e) => e !== id)
          : [...prev, id],
    );
  }

  function selectProvider(next: string) {
    onProviderChange?.();
    setProvider(next);
    setEventIds([]);
    setResource(null);
    setTopic("");
    setTitleEdit(null);
    setDescriptionEdit(null);
    // Resource feeds are per-provider — clearing re-arms the lazy fetch for
    // the newly selected one.
    setResourceOptions(null);
    setResourceError(null);
  }

  async function submit(): Promise<FromIntegrationResult | null> {
    if (!provider || eventIds.length === 0) return null;
    setSubmitting(true);
    setErr(null);
    try {
      const body = {
        provider,
        // One trigger, every ticked event. The backend still accepts the old
        // singular `event_id`, but nothing here sends it.
        event_ids: eventIds,
        // Only the auto-register shapes carry a resource; guided providers'
        // bodies stay exactly as before.
        ...(resource ? { resource } : {}),
        // Only the owner_setup shape (gmail.message) carries a topic.
        ...(isOwnerSetup && topic.trim() ? { topic: topic.trim() } : {}),
        // Only an attach carries a target — the create path's body is
        // byte-for-byte what it always was.
        ...(routineId ? { routine_id: routineId } : {}),
        // Overrides ONLY when the owner actually changed the draft (and only
        // where the draft is even used — an attach never rewrites the target's
        // title or instructions). An untouched draft sends nothing, so the
        // registry template renders server-side exactly as it always has.
        ...(routineId == null && titleEdit != null && titleEdit.trim() !== draftTitle
          ? { title: titleEdit.trim() }
          : {}),
        ...(routineId == null &&
        descriptionEdit != null &&
        descriptionEdit.trim() !== draftDescription
          ? { description: descriptionEdit.trim() }
          : {}),
      };
      const out = (await api.post("/automations/from-integration", body)) as FromIntegrationResult;
      setResult(out);
      return out;
    } catch (e) {
      if (notifyConversionGate(e) === null) setErr(gateMessage(e));
      return null;
    } finally {
      setSubmitting(false);
    }
  }

  async function saveWebhookSecret() {
    if (!result || whsecInput.trim() === "") return;
    setWhsecSubmitting(true);
    setErr(null);
    try {
      await api.put(`/automations/${result.trigger_public_id}/webhook-secret`, {
        webhook_secret: whsecInput.trim(),
      });
      setWhsecSaved(true);
    } catch (e) {
      if (notifyConversionGate(e) === null) setErr(gateMessage(e));
    } finally {
      setWhsecSubmitting(false);
    }
  }

  return {
    provider,
    selectProvider,
    eventIds,
    toggleEvent,
    multiSelect,
    events,
    selectedEvents,
    triggerableConnections,
    selectedConnection,
    selectedEvent,
    isAutoRegister,
    isOwnerSetup,
    effectiveRegistration,
    attachDisclosure,
    resource,
    setResource,
    resourceOptions,
    resourceError,
    resourceNoun,
    topic,
    setTopic,
    title,
    setTitleEdit,
    description,
    setDescriptionEdit,
    // Title/instructions belong to the automation, so they are only editable
    // where this call actually writes them: the create path.
    showDraftFields: routineId == null && selectedEvent != null && template != null,
    result,
    submitting,
    err,
    setErr,
    canSubmit,
    submit,
    whsecInput,
    setWhsecInput,
    whsecSubmitting,
    whsecSaved,
    saveWebhookSecret,
  };
}

export type ConnectedAppTriggerCtl = ReturnType<typeof useConnectedAppTrigger>;

// Searchable MULTI-select for the event catalog. Providers carry their FULL
// event lists (Stripe ~265), so a plain dropdown stopped scaling. With no
// filter and a big catalog: a "Popular" group (the first PINNED_EVENT_COUNT
// registry entries — providers.yaml orders the curated picks first) then the
// rest alphabetized. Typing collapses to one flat list matched
// case-insensitively against label AND id. Small catalogs (≤8) skip the
// grouping and list in registry order.
//
// The list lives in a POPOVER, not inline. Inline it was 260px of always-open
// scroller wedged into a dialog that doesn't scroll, so every field below it
// sat past the viewport edge with the only scrollable thing under the cursor
// being the event list itself — the form became unreachable after picking an
// app. A popover also puts the list on its own dismissable layer, which is
// what makes Escape close the LIST rather than the whole dialog (Radix
// dismisses the topmost layer and stops there), instead of discarding every
// selection made so far.
const PINNED_EVENT_COUNT = 3;

/** The trigger button's summary of what is ticked. Two fit; beyond that the
 * first plus a count, so the button never wraps out of the dialog. */
function eventSummary(labels: string[]): string | null {
  if (labels.length === 0) return null;
  if (labels.length <= 2) return labels.join(", ");
  return `${labels[0]} +${labels.length - 1} more`;
}

function EventPicker({
  events,
  values,
  onToggle,
  multiple,
}: {
  events: { id: string; label: string }[];
  values: string[];
  onToggle: (id: string) => void;
  /** False for a provider whose deliveries name the event only in a header
   * (GitHub, Sentry) — one event per trigger, so this behaves exactly as the
   * pre-multi-select picker did: pick one, the list closes. */
  multiple: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  // Roving focus over the options (same idiom as the rung radiogroup): Arrow
  // keys move focus, Enter/Space activate the focused option button natively.
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const q = query.trim().toLowerCase();
  const filtered = q
    ? events.filter(
        (e) => e.label.toLowerCase().includes(q) || e.id.toLowerCase().includes(q),
      )
    : null;
  const grouped = filtered === null && events.length > 8;
  const popular = grouped ? events.slice(0, PINNED_EVENT_COUNT) : [];
  const rest = grouped
    ? [...events.slice(PINNED_EVENT_COUNT)].sort((a, b) => a.id.localeCompare(b.id))
    : (filtered ?? events);
  const visible = [...popular, ...rest];
  const selectedIndex = visible.findIndex((e) => values.includes(e.id));

  function onListKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const i = optionRefs.current.findIndex((el) => el === document.activeElement);
    const next =
      e.key === "ArrowDown" ? Math.min(i + 1, visible.length - 1) : Math.max(i - 1, 0);
    optionRefs.current[next]?.focus();
  }

  const groupHeading = (text: string) => (
    <div className="px-3 pt-2 pb-1 label-caps text-[var(--midnight-muted)]">
      {text}
    </div>
  );

  const selectedLabel = eventSummary(
    values.map((id) => events.find((e) => e.id === id)?.label ?? id),
  );

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="flex w-full items-center justify-between gap-2 rounded-sm border border-[var(--edge)] bg-[var(--cream)] px-3 py-2 text-left text-sm text-[var(--midnight)]"
        >
          <span className={cn("truncate", !selectedLabel && "text-[var(--midnight-muted)]")}>
            {selectedLabel ?? (multiple ? "Select events…" : "Select an event…")}
          </span>
          <ChevronDown size={16} className="shrink-0 text-[var(--midnight-muted)]" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-[var(--radix-popover-trigger-width)] p-2"
        // The search input owns focus on open, so typing filters immediately
        // instead of landing on the trigger's own focus restore.
        onOpenAutoFocus={(e) => {
          e.preventDefault();
          searchRef.current?.focus();
        }}
      >
        <input
          ref={searchRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search events…"
          aria-label="Search events"
          className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 text-[var(--midnight)]"
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              optionRefs.current[0]?.focus();
            }
          }}
        />
        <div
          role="listbox"
          aria-label={multiple ? "Events" : "Event"}
          aria-multiselectable={multiple || undefined}
          onKeyDown={onListKeyDown}
          className="mt-1 max-h-[260px] overflow-y-auto rounded-sm border border-[var(--edge)] bg-[var(--cream)] pb-1"
        >
          {visible.length === 0 && (
            <p className="px-3 py-2 text-sm text-[var(--midnight-muted)]">
              No events match &ldquo;{query.trim()}&rdquo;.
            </p>
          )}
          {visible.map((e, i) => (
            <div key={e.id}>
              {grouped && i === 0 && groupHeading("Popular")}
              {grouped && i === popular.length && groupHeading("All events")}
              <button
                ref={(el) => {
                  optionRefs.current[i] = el;
                }}
                type="button"
                role="option"
                aria-selected={values.includes(e.id)}
                tabIndex={i === (selectedIndex === -1 ? 0 : selectedIndex) ? 0 : -1}
                onClick={() => {
                  // Closing on the FIRST pick keeps single-select exactly as
                  // effortless as it was: tick one, the list gets out of the
                  // way. Once something is ticked you are clearly building a
                  // set, so the list stays put for the next one — unless this
                  // provider only takes one event, where it always closes.
                  const wasEmpty = values.length === 0;
                  onToggle(e.id);
                  if (!multiple || wasEmpty) setOpen(false);
                }}
                className={cn(
                  "flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm transition-colors",
                  values.includes(e.id)
                    ? "bg-[var(--cream-deeper)] text-[var(--midnight)]"
                    : "text-[var(--midnight-soft)] hover:bg-[var(--cream-deep)]",
                )}
              >
                {/* The tick box is what tells the owner several events can
                    share this trigger — so it only appears where they can. */}
                {multiple && (
                  <span
                    aria-hidden="true"
                    className={cn(
                      "flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-sm border",
                      values.includes(e.id)
                        ? "border-[var(--midnight)] bg-[var(--midnight)] text-[var(--cream)]"
                        : "border-[var(--edge)]",
                    )}
                  >
                    {values.includes(e.id) && <Check size={12} strokeWidth={3} />}
                  </span>
                )}
                <span className="truncate">{e.label}</span>
                {!multiple && values.includes(e.id) && (
                  <Check size={16} strokeWidth={2} className="ml-auto shrink-0 text-[var(--midnight)]" />
                )}
              </button>
            </div>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

/**
 * The form: app -> event -> (resource / topic) -> what to call it and what it
 * should do. `children` slots in between the wiring and the draft — the create
 * dialog puts its "which automation?" target picker there.
 */
export function ConnectedAppFields({
  ctl,
  agents,
  children,
}: {
  ctl: ConnectedAppTriggerCtl;
  agents: CompanyAgent[];
  children?: React.ReactNode;
}) {
  return (
    <>
      <label className="block">
        <span className="text-sm text-[var(--midnight-soft)]">Connected app</span>
        <div className="mt-1">
          <EditorialSelect
            options={ctl.triggerableConnections.map((c) => ({
              value: c.provider,
              label: c.display_name,
            }))}
            value={ctl.provider ?? undefined}
            onValueChange={ctl.selectProvider}
            placeholder={
              ctl.triggerableConnections.length === 0
                ? "No connected apps can trigger automations yet"
                : "Select an app…"
            }
            disabled={ctl.triggerableConnections.length === 0}
          />
        </div>
      </label>
      {ctl.selectedConnection && (
        <div className="block">
          <span className="text-sm text-[var(--midnight-soft)]">
            {ctl.multiSelect ? "Events" : "Event"}
          </span>
          <div className="mt-1">
            {/* Keyed by provider so switching apps resets the filter. */}
            <EventPicker
              key={ctl.provider}
              events={ctl.events}
              values={ctl.eventIds}
              onToggle={ctl.toggleEvent}
              multiple={ctl.multiSelect}
            />
          </div>
        </div>
      )}
      {/* The auto-register shapes' third step — which repo/project to hook.
          The backend registers the webhook there itself, so unlike guided
          providers there is no paste step after Create. */}
      {ctl.isAutoRegister && ctl.eventIds.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <label className="block">
            <span className="text-sm text-[var(--midnight-soft)]">{ctl.resourceNoun.label}</span>
            <div className="mt-1">
              <EditorialSelect
                options={(ctl.resourceOptions ?? []).map((r) => ({
                  value: r.id,
                  label: r.label,
                }))}
                value={ctl.resource ?? undefined}
                onValueChange={ctl.setResource}
                placeholder={
                  ctl.resourceError
                    ? `Couldn't load ${ctl.resourceNoun.plural}`
                    : ctl.resourceOptions === null
                      ? `Loading ${ctl.resourceNoun.plural}…`
                      : ctl.resourceOptions.length === 0
                        ? `No ${ctl.resourceNoun.plural} found`
                        : `Select a ${ctl.resourceNoun.label.toLowerCase()}…`
                }
                disabled={ctl.resourceOptions === null || ctl.resourceOptions.length === 0}
              />
            </div>
            {ctl.resourceError && (
              <span className="text-xs text-[var(--err)]">{ctl.resourceError}</span>
            )}
          </label>
          {/* Google's "resource" is an identity, not a repo — which agent
              reads whose mailbox. That deserves a stated consequence and a
              reconciliation with the per-agent assignment, neither of which a
              bare dropdown carries. A sibling of the <label>, not a child: it
              holds its own buttons, which a label would hijack. */}
          {ctl.provider === "google" && (
            <AutomationGoogleAccount
              agents={agents}
              account={ctl.resource}
              accountOptions={ctl.resourceOptions}
              onSelectAccount={ctl.setResource}
            />
          )}
        </div>
      )}
      {/* owner_setup (gmail.message) — the owner's own Pub/Sub topic. The
          one-time topic/subscription wiring instructions come back in the
          finish dialog after Create. */}
      {ctl.isOwnerSetup && (
        <label className="block">
          <span className="text-sm text-[var(--midnight-soft)]">Pub/Sub topic</span>
          <input
            value={ctl.topic}
            onChange={(e) => ctl.setTopic(e.target.value)}
            placeholder="projects/my-project/topics/isol8-gmail"
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 font-mono text-[var(--midnight)]"
          />
          <span className="text-xs text-[var(--midnight-muted)]">
            A topic in your own Google Cloud project — you&apos;ll wire it up after Create.
          </span>
        </label>
      )}
      {children}
      {/* Title + instructions, pre-filled with a draft written from the chosen
          app and event. Editable: wiring Stripe used to hand you boilerplate
          with no way to say what you actually wanted done. Untouched, nothing
          is sent and the server renders the same template it always did. */}
      {ctl.showDraftFields && (
        <>
          <label className="block">
            <span className="text-sm text-[var(--midnight-soft)]">Title</span>
            <input
              value={ctl.title}
              onChange={(e) => ctl.setTitleEdit(e.target.value)}
              className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
            />
          </label>
          <label className="block">
            <span className="text-sm text-[var(--midnight-soft)]">Description</span>
            <textarea
              value={ctl.description}
              onChange={(e) => ctl.setDescriptionEdit(e.target.value)}
              rows={4}
              className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
            />
          </label>
          <span className="-mt-2 text-xs text-[var(--midnight-muted)]">
            What the agent is told to do when this fires. Edit it — this draft is a starting
            point, not a fixed template.
          </span>
        </>
      )}
      {ctl.attachDisclosure && (
        <p
          className="rounded-sm px-3 py-2 text-xs text-[var(--midnight-soft)]"
          style={{
            border: "1px solid var(--edge)",
            background: "var(--cream-deep)",
          }}
        >
          {ctl.attachDisclosure}
        </p>
      )}
    </>
  );
}

// Every guided provider's providers.yaml `instructions` template splices in
// the literal `{proxy_url}` exactly once (Stripe/Sentry/Linear/Attio/
// PostHog). Rendered as plain text that URL is one unbroken token that
// overflows past the dialog's edge instead of wrapping, so pull it into its
// own break-all span — matching SecretField's own convention for raw
// URLs/secrets — and leave the rest of the (already-wrapping) sentence alone.
// Falls back to the plain string if the URL isn't found in it.
function renderInstructions(instructions: string, proxyUrl: string | null) {
  const idx = proxyUrl ? instructions.indexOf(proxyUrl) : -1;
  if (idx === -1 || !proxyUrl) return instructions;
  return (
    <>
      {instructions.slice(0, idx)}
      <code className="break-all">{proxyUrl}</code>
      {instructions.slice(idx + proxyUrl.length)}
    </>
  );
}

/** The finish dialog: what the owner still has to do (if anything) once the
 * trigger exists. Replaces the form — both surfaces render it instead of their
 * own dialog once `ctl.result` lands. */
export function ConnectedAppResultDialog({
  ctl,
  onDone,
}: {
  ctl: ConnectedAppTriggerCtl;
  onDone: () => void;
}) {
  const result = ctl.result;
  if (!result) return null;
  const providerName = ctl.selectedConnection?.display_name ?? ctl.provider ?? "the app";
  // Prefer the picker's human label for the registered resource — the backend
  // echoes the id it hooked (a GitHub "owner/repo" reads fine, but a Vercel
  // "prj_…" id doesn't), and the picker already knows the name.
  const resourceLabel = result.resource
    ? (ctl.resourceOptions?.find((r) => r.id === result.resource)?.label ?? result.resource)
    : null;
  // Guided (Stripe) results aren't armed until the owner pastes the whsec back
  // via saveWebhookSecret — closing before that leaves an active, unsigned
  // automation with no UI left to finish it (the detail panel treats a
  // "none"-signing trigger as non-rotatable). Auto (GitHub/Vercel) results are
  // already fully armed by the time this dialog shows. Reverse-guided
  // (webhook_secret present) results have no paste-back — the trigger is born
  // armed with its own secret and the owner wires the provider side by hand —
  // so Done is never gated for them either.
  const isReverseGuided = !!result.webhook_secret;
  const mustSaveSecretFirst = result.guided && !isReverseGuided && !ctl.whsecSaved;
  // Poll results (gmail.poll) are schedule-backed: nothing to wire, no URL.
  const isPoll = result.poll === true;
  // Non-guided results carrying instructions (gmail.message) still need a
  // one-time owner-side setup — informational only, Done is never gated.
  const needsOwnerSetup = !result.guided && !isPoll && !!result.instructions;
  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && !mustSaveSecretFirst && onDone()}
      title={result.guided || needsOwnerSetup ? "Finish setup" : "Automation registered"}
      description={
        result.guided
          ? isReverseGuided
            ? `Copy this URL and Authorization header value into ${providerName} — the secret is shown only once.`
            : `Paste this URL into ${providerName}'s webhook settings, then paste the signing secret it gives you back here.`
          : isPoll
            ? `Checks ${resourceLabel ?? "the account"}'s inbox every 10 minutes — this automation is live.`
            : needsOwnerSetup
              ? "One-time setup in your Google Cloud project points push notifications at this URL — follow the steps below."
              : `${providerName} registered the webhook${resourceLabel ? ` on ${resourceLabel}` : " automatically"} — this automation is live.`
      }
      footer={
        <EditorialButton
          variant="primary"
          size="sm"
          onClick={onDone}
          disabled={mustSaveSecretFirst}
          title={mustSaveSecretFirst ? "Save the signing secret first" : undefined}
        >
          Done
        </EditorialButton>
      }
    >
      <div className="flex flex-col gap-3">
        {/* Poll results have no webhook — nothing to copy. */}
        {result.proxy_url && <SecretField label="Webhook URL" value={result.proxy_url} />}
        {/* One endpoint, several events: the dashboard step is "tick all of
            these on that one URL", so name them rather than leaving the owner
            to re-derive the list from the instructions sentence. */}
        {result.guided && ctl.selectedEvents.length > 1 && (
          <div className="flex flex-col gap-1">
            <span className="text-sm text-[var(--midnight-soft)]">
              Subscribe this one endpoint to all {ctl.selectedEvents.length} events
            </span>
            <ul className="flex flex-col gap-0.5">
              {ctl.selectedEvents.map((e) => (
                <li key={e.id} className="text-xs text-[var(--midnight-muted)]">
                  {e.label} — <code className="break-all">{e.id}</code>
                </li>
              ))}
            </ul>
          </div>
        )}
        {/* Reverse-guided: the FULL header value ("Bearer <secret>"), not the
            bare secret — copying it whole eliminates the classic
            forgot-the-Bearer-prefix failure. */}
        {result.webhook_secret && (
          <SecretField
            label="Authorization header value"
            value={`Bearer ${result.webhook_secret}`}
          />
        )}
        {result.instructions && (
          <p className="text-xs text-[var(--midnight-muted)]">
            {renderInstructions(result.instructions, result.proxy_url)}
          </p>
        )}
        {result.guided && !isReverseGuided ? (
          <>
            <label className="block">
              <span className="text-sm text-[var(--midnight-soft)]">
                Paste your {providerName} signing secret
              </span>
              <input
                type="password"
                value={ctl.whsecInput}
                onChange={(e) => ctl.setWhsecInput(e.target.value)}
                placeholder={result.secret_hint ?? "Signing secret"}
                className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 font-mono text-[var(--midnight)]"
              />
            </label>
            {ctl.whsecSaved ? (
              <p className="text-sm" style={{ color: "var(--midnight)" }}>
                Secret saved.
              </p>
            ) : (
              <EditorialButton
                variant="secondary"
                size="sm"
                disabled={ctl.whsecSubmitting || ctl.whsecInput.trim() === ""}
                onClick={ctl.saveWebhookSecret}
              >
                {ctl.whsecSubmitting ? "Saving…" : "Save secret"}
              </EditorialButton>
            )}
          </>
        ) : (
          // Reverse-guided is guided-but-unwired: "Registered." would claim a
          // liveness the owner's PostHog-side setup hasn't earned yet.
          !result.guided && (
            <p className="text-sm" style={{ color: "var(--midnight)" }}>
              Registered.
            </p>
          )
        )}
        {ctl.err && (
          <div
            className="text-[var(--err)] text-sm rounded-sm px-3 py-2"
            style={{
              border: "1px solid var(--err)",
              background: "var(--cream)",
            }}
          >
            {ctl.err}
          </div>
        )}
      </div>
    </EditorialDialog>
  );
}
