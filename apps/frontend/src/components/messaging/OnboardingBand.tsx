"use client";

import { useCallback, useEffect, useMemo, useState, useSyncExternalStore, type ReactElement } from "react";
import { Check, Cpu, FileText, MessageSquare, Users, Pencil, Plus, X } from "lucide-react";
import useSWR from "swr";
import { useOrganization } from "@clerk/nextjs";
import { useRouter } from "next/navigation";

import { useApi } from "@/lib/api";
import { useConnections } from "@/hooks/useConnections";
import { useBilling } from "@/hooks/useBilling";
import { navigateShell } from "@/lib/shellNav";
import { PROVIDER_PICKER } from "@/components/chat/ChatErrorCard";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { buildSetupSteps, isBandComplete, isConnectorStep, setupProgress, type SetupStep, type SetupStepId } from "@lightbulb/shared";
import { openInviteDialog } from "@/components/workspace/workspaceController";
import { ConnectorIcon } from "./ConnectorIcons";
import { RoomInviteCard } from "./RoomInviteCard";
import type { RoomParticipant } from "./types";

/** `GET /company/context` — whether the CEO's Hermes memory holds pasted context. */
type ContextStatus = { imported: boolean };

/** Mirrors the backend's `MAX_CONTEXT_CHARS`; longer pastes are refused, not truncated. */
const MAX_CONTEXT_CHARS = 200_000;

// =============================================================================
// The onboarding setup band — Slack's "Get started by setting up the channel
// for your team" cards, in the starter channel only.
//
// It renders in RoomTimeline's `leading` slot, beside RoomIntro, so it sits
// above the oldest message rather than being one. That means it is never part
// of history: it cannot be replied to, searched, or left behind by a rename,
// and it disappears the moment it has nothing left to ask for.
//
// Nothing here is persisted. Every step's done-state is derived in
// `onboardingSteps.ts` from the room and the connections catalog, so the only
// stored bit in the whole feature is the per-viewer dismissal below.
// =============================================================================

/**
 * Per WORKSPACE, not per browser. A viewer can belong to several Clerk orgs,
 * and dismissing the band in a workspace they finished setting up must not
 * suppress it in a brand-new one that still needs every step. `sidebarPrefs.ts`
 * scopes its own key the same way.
 */
const DISMISS_KEY_PREFIX = "lightbulb.messaging.onboarding-band.dismissed.";

const dismissKey = (ownerId: string) => `${DISMISS_KEY_PREFIX}${ownerId}`;
/** Set the first time the band renders complete: "All set" shows for that one session, then goes. */
const allSetKey = (ownerId: string) => `lightbulb.messaging.onboarding-band.all-set.${ownerId}`;

/** Why a card is inert, in the words of the person looking at it. */
const NON_ADMIN_HINT = "Ask an admin";

function readDismissed(ownerId: string): boolean {
  if (typeof window === "undefined" || !ownerId) return false;
  try {
    return window.localStorage.getItem(dismissKey(ownerId)) === "1";
  } catch {
    return false;
  }
}

function writeDismissed(ownerId: string): void {
  if (!ownerId) return;
  try {
    window.localStorage.setItem(dismissKey(ownerId), "1");
  } catch {
    // A viewer with storage blocked simply sees the band again next session.
  }
}

interface OnboardingBandProps {
  participants: readonly RoomParticipant[];
  /** A person has written in this channel ("Tell your CEO about the business"). */
  humanSpoke?: boolean;
}

export function OnboardingBand({ participants, humanSpoke = false }: OnboardingBandProps): ReactElement | null {
  const { connections, startOAuth, isLoading: connectionsLoading, error: connectionsError } = useConnections();
  const { organization, membership } = useOrganization();
  const router = useRouter();
  const teams = useTeamsApi();
  // Same SWR key MessagingWorkspaceContext already reads, so this shares its
  // cache entry rather than issuing a second request.
  const { data: roster, isLoading: rosterLoading, mutate: refreshRoster } =
    teams.read<{ agents?: CompanyAgent[] }>("/agents");
  const api = useApi();
  const { account: billing, isLoading: billingLoading } = useBilling();

  // localStorage read through useSyncExternalStore rather than an effect:
  // `react-hooks/set-state-in-effect` is an ERROR in this repo, and the server
  // snapshot (`false`) keeps hydration honest instead of producing a mismatch.
  // Nothing outside this component writes the key, so `subscribe` is a no-op
  // and the in-session dismissal below drives the re-render.
  const ownerId = organization?.id ?? "";
  const subscribe = useCallback(() => () => {}, []);
  const readScoped = useCallback(() => readDismissed(ownerId), [ownerId]);
  const storedDismissal = useSyncExternalStore(subscribe, readScoped, () => false);
  const [dismissedNow, setDismissedNow] = useState(false);

  // Derived from the CEO's profile (a marker the paste writes), not stored per
  // viewer. Keyed by workspace so switching orgs re-reads it.
  const { data: context, error: contextError, isLoading: contextLoading, mutate: setContext } = useSWR(
    ownerId ? ["company-context", ownerId] : null,
    () => api.get("/company/context") as Promise<ContextStatus>,
  );
  const [sharing, setSharing] = useState(false);

  const [naming, setNaming] = useState(false);
  const [connecting, setConnecting] = useState<SetupStepId | null>(null);
  const [connectFailed, setConnectFailed] = useState<SetupStepId | null>(null);

  const agentNames = useMemo(
    () => new Map((roster?.agents ?? []).map((agent) => [agent.id, agent.name] as const)),
    [roster],
  );
  const workspaceMembers = organization?.membersCount ?? 0;
  const steps = useMemo(
    () => buildSetupSteps({
      participants, connections, agentNames, workspaceMembers, billing,
      // A failed read drops the card rather than showing one that lies.
      contextImported: contextError ? null : context?.imported,
      humanSpoke,
    }),
    [participants, connections, agentNames, workspaceMembers, billing, context, contextError, humanSpoke],
  );
  const complete = isBandComplete(steps);
  // Read once per mount: the session that first sees the band complete keeps "All set" up.
  const [allSetSeen] = useState(() => {
    try { return !!ownerId && window.localStorage.getItem(allSetKey(ownerId)) === "1"; } catch { return false; }
  });
  useEffect(() => {
    if (!complete || !ownerId) return;
    try { window.localStorage.setItem(allSetKey(ownerId), "1"); } catch { /* storage blocked: shown again next session */ }
  }, [complete, ownerId]);

  const isAdmin = membership?.role === "org:admin";

  const onStep = useCallback((step: SetupStep) => {
    if (step.id === "invite") return openInviteDialog();
    if (step.id === "tell-ceo") return document.querySelector<HTMLTextAreaElement>(".room-composer textarea")?.focus();
    if (step.id === "name-agents") return setNaming((open) => !open);
    if (step.id === "context") return setSharing(true);
    if (step.id === "starter-model") return navigateShell(PROVIDER_PICKER);
    // startOAuth rethrows on purpose (403, a failed browser open), and on
    // desktop it stays pending until the grant lands — so the card shows both.
    setConnecting(step.id);
    setConnectFailed(null);
    startOAuth(step.id)
      .catch(() => setConnectFailed(step.id))
      .finally(() => setConnecting(null));
  }, [startOAuth]);

  // Both reads must have ANSWERED before any judgement about what is left to
  // do. While they are in flight `connections` is [] and `roster` undefined,
  // which derives "nothing is done" — so a fully set-up workspace would flash
  // the whole band on every cold load and then have it vanish.
  // A failed catalog read would derive every connector as outstanding.
  // Billing only has to settle, not succeed: a failed read just omits its card.
  if (connectionsLoading || connectionsError || rosterLoading || billingLoading || contextLoading || !ownerId) return null;
  if (storedDismissal || dismissedNow) return null;
  if (complete) {
    return allSetSeen ? null : (
      <section className="onboarding-band" aria-label="Set up your workspace">
        <p>All set. Your team can see everything your AI employees do.</p>
      </section>
    );
  }

  const { done, total } = setupProgress(steps);
  const byId = (id: SetupStepId) => steps.find((step) => step.id === id);
  const starter = byId("starter-model");
  const invite = byId("invite");
  const connectors = steps.filter(isConnectorStep);
  const core = (["tell-ceo"] as const).map(byId).filter((step): step is SetupStep => !!step);
  const later = steps.filter((step) => step.later);

  return (
    <section className="onboarding-band" aria-label="Set up your workspace">
      <header className="onboarding-band-head">
        <div>
          <h3>Get started · {done} of {total} done</h3>
          <p>Bring in the person you work with most, then give Lightbulb one thing to do.</p>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="onboarding-band-dismiss"
          aria-label="Dismiss setup"
          onClick={() => {
            writeDismissed(ownerId);
            setDismissedNow(true);
          }}
        >
          <X size={16} aria-hidden="true" />
        </Button>
      </header>

      {starter && (
        <div className="onboarding-band-actions">
          <StepCard step={starter} isAdmin={isAdmin} wide icon={<Cpu size={16} aria-hidden="true" />} onSelect={onStep} />
        </div>
      )}

      <div className="onboarding-band-actions">
        <EndowedCard label="Your workspace" detail={`${organization?.name || "Your workspace"} is ready`} />
        <EndowedCard label="Your AI employees" detail="Six of them, in #general" />
      </div>

      {invite && (invite.done ? (
        <div className="onboarding-band-actions">
          <StepCard step={invite} isAdmin={isAdmin} wide icon={ACTION_ICONS.invite} onSelect={onStep} />
        </div>
      ) : (
        <div className="onboarding-band-invite">
          <span className="onboarding-card-label"><Users size={16} aria-hidden="true" />{invite.label}</span>
          <span className="onboarding-card-detail">{invite.detail}</span>
          <RoomInviteCard source="onboarding_band" offerJustMe={false} />
        </div>
      ))}

      <div className="onboarding-band-actions">
        {core.slice(0, 1).map((step) => (
          <StepCard key={step.id} step={step} isAdmin={isAdmin} wide icon={ACTION_ICONS[step.id]} onSelect={onStep} />
        ))}
      </div>

      {connectors.length > 0 && <p className="onboarding-card-label">Connect one tool</p>}
      <div className="onboarding-band-grid">
        {connectors.map((step) => (
          <StepCard
            key={step.id}
            step={step}
            isAdmin={isAdmin}
            icon={<ConnectorIcon provider={step.id} />}
            busy={connecting === step.id}
            status={connecting === step.id ? "Connecting…" : connectFailed === step.id ? "Couldn’t connect — try again" : undefined}
            onSelect={onStep}
          />
        ))}
        {/* The fourth tile is a door, not a task. It is NOT a SetupStep — see
            onboardingSteps.ts — so it never counts toward completion, and it
            stays live for a member who cannot connect anything themselves:
            seeing what the workspace is already wired to is not a mutation. */}
        <button
          type="button"
          className="onboarding-card"
          data-more="true"
          onClick={() => router.push("/settings?panel=connections")}
        >
          <Plus size={26} aria-hidden="true" strokeWidth={1.75} />
          <span className="onboarding-card-label">See more connections</span>
        </button>
      </div>

      <div className="onboarding-band-actions">
        {core.slice(1).map((step) => (
          <StepCard key={step.id} step={step} isAdmin={isAdmin} wide icon={ACTION_ICONS[step.id]} onSelect={onStep} />
        ))}
      </div>

      {later.length > 0 && <p className="onboarding-card-label">Later</p>}
      <div className="onboarding-band-actions">
        {later.map((step) => (
          <StepCard key={step.id} step={step} isAdmin={isAdmin} wide icon={ACTION_ICONS[step.id]} onSelect={onStep} />
        ))}
      </div>

      <ContextDialog
        open={sharing}
        // Flip the card only once the dialog closes: marking it done while the
        // success state is on screen could retire the whole band under it.
        onClose={(saved) => {
          setSharing(false);
          if (saved) void setContext({ imported: true }, { revalidate: false });
        }}
      />

      {naming && (
        <AgentNames
          participants={participants}
          agentNames={agentNames}
          onRefresh={refreshRoster}
          onDone={() => setNaming(false)}
        />
      )}
    </section>
  );
}

const ACTION_ICONS: Partial<Record<SetupStepId, ReactElement>> = {
  invite: <Users size={16} aria-hidden="true" />,
  "tell-ceo": <MessageSquare size={16} aria-hidden="true" />,
  "name-agents": <Pencil size={16} aria-hidden="true" />,
  context: <FileText size={16} aria-hidden="true" />,
};

/** Endowed progress (08 §3): always done, never clickable. */
function EndowedCard({ label, detail }: { label: string; detail: string }): ReactElement {
  return (
    <div className="onboarding-card" data-wide="true" data-done="true">
      <span className="onboarding-card-label"><Check size={16} aria-hidden="true" />{label}</span>
      <span className="onboarding-card-detail">{detail}</span>
    </div>
  );
}

function StepCard({ step, isAdmin, onSelect, wide, icon, status, busy }: {
  step: SetupStep;
  isAdmin: boolean;
  onSelect: (step: SetupStep) => void;
  wide?: boolean;
  icon?: ReactElement;
  status?: string;
  busy?: boolean;
}): ReactElement {
  const blocked = step.adminOnly && !isAdmin;
  // A done card stays on screen until the whole band goes, so the row does not
  // reflow under the cursor as each one lands. It goes inert only if repeating
  // it is meaningless — see SetupStep.repeatable.
  const detail = step.done ? step.doneDetail : blocked ? NON_ADMIN_HINT : status ?? step.detail;
  return (
    <button
      type="button"
      className="onboarding-card"
      data-wide={wide ? "true" : undefined}
      data-done={step.done ? "true" : undefined}
      disabled={blocked || busy || (step.done && !step.repeatable)}
      onClick={() => onSelect(step)}
    >
      <span className="onboarding-card-label">
        {step.done ? <Check size={16} aria-hidden="true" /> : icon}
        {step.label}
      </span>
      <span className="onboarding-card-detail">{detail}</span>
    </button>
  );
}

/**
 * The starter six, editable in place. Only changed rows are sent, so saving
 * without touching anything is a no-op rather than six redundant PATCHes.
 */
function AgentNames({ participants, agentNames, onRefresh, onDone }: {
  participants: readonly RoomParticipant[];
  agentNames: ReadonlyMap<string, string>;
  onRefresh: () => Promise<unknown>;
  onDone: () => void;
}): ReactElement {
  const api = useApi();
  const agents = useMemo(
    () => participants
      .filter((participant) => participant.kind === "agent")
      .map((agent) => ({ id: agent.id, name: agentNames.get(agent.id) ?? agent.displayName })),
    [participants, agentNames],
  );
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    const changed = agents.filter((agent) => {
      const next = draft[agent.id]?.trim();
      return next && next !== agent.name;
    });
    if (changed.length === 0) return onDone();

    setSaving(true);
    setError(null);
    try {
      // allSettled, not all: a rejection mid-flight would otherwise leave the
      // successful renames invisible AND unreported. Every outcome is counted
      // so the message can say how many of them landed.
      const results = await Promise.allSettled(changed.map((agent) =>
        api.patch(`/teams/agents/${encodeURIComponent(agent.id)}`, { name: draft[agent.id].trim() }),
      ));
      const failed = results.filter((result) => result.status === "rejected").length;
      // Refresh either way: on a partial failure the ones that DID save must
      // still show their new names, in the panel and in the band's done-state.
      await onRefresh();
      if (failed === 0) return onDone();
      setError(failed === changed.length
        ? "Couldn’t save those names. Try again."
        : `${failed} of ${changed.length} names didn’t save. Try again.`);
    } catch {
      setError("Couldn’t save those names. Try again.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="onboarding-names">
      <p>
        Your team ships with role names. Give them real ones — the agents answer to whatever you call them.
      </p>
      <ul>
        {agents.map((agent) => (
          <li key={agent.id}>
            {/* The agent's current name identifies which field is which. */}
            <label htmlFor={`onboarding-name-${agent.id}`}>{agent.name}</label>
            <Input
              id={`onboarding-name-${agent.id}`}
              // Controlled, so a roster that resolves AFTER this panel opened
              // replaces the room member row's stale snapshot in the field
              // instead of leaving a name the agent no longer has on screen.
              value={draft[agent.id] ?? agent.name}
              maxLength={80}
              autoComplete="off"
              onChange={(event) => setDraft((current) => ({ ...current, [agent.id]: event.target.value }))}
            />
          </li>
        ))}
      </ul>
      {error && <p className="onboarding-names-error" role="alert">{error}</p>}
      <Button type="button" onClick={() => void save()} disabled={saving}>
        {saving ? "Saving…" : "Save names"}
      </Button>
    </div>
  );
}

/**
 * Paste (or load a .md/.txt) once; `POST /company/context` merges it into the
 * CEO's Hermes `memories/USER.md`. The file never uploads — it only fills the
 * textarea, so what is saved is exactly what the owner can see.
 */
function ContextDialog({ open, onClose }: { open: boolean; onClose: (saved: boolean) => void }): ReactElement {
  const api = useApi();
  const [text, setText] = useState("");
  const [state, setState] = useState<"idle" | "saving" | "saved">("idle");
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    onClose(state === "saved");
    setText("");
    setState("idle");
    setError(null);
  };

  const loadFile = async (file: File | undefined) => {
    if (!file) return;
    const body = await file.text();
    if (body.length > MAX_CONTEXT_CHARS) return setError("That file is too long. Paste the parts that matter most.");
    setError(null);
    setText(body);
  };

  const save = async () => {
    setState("saving");
    setError(null);
    try {
      await api.post("/company/context", { text });
      setState("saved");
    } catch {
      setState("idle");
      setError("Couldn’t save that. Try again.");
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) close(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Share your context</DialogTitle>
          <DialogDescription>
            {state === "saved"
              ? "Lightbulb has it and will use it from now on."
              : "Paste what you’ve already told Claude or ChatGPT about your business. It goes to Lightbulb’s memory, which you can edit later in Files."}
          </DialogDescription>
        </DialogHeader>
        {state !== "saved" && (
          <>
            <Textarea
              aria-label="Your context"
              placeholder="Paste what you’ve already told Claude or ChatGPT about your business"
              value={text}
              maxLength={MAX_CONTEXT_CHARS}
              rows={10}
              onChange={(event) => setText(event.target.value)}
            />
            <label className="onboarding-context-file">
              Or load a file
              <input
                type="file"
                accept=".md,.txt,text/markdown,text/plain"
                onChange={(event) => void loadFile(event.target.files?.[0])}
              />
            </label>
            {error && <p className="onboarding-names-error" role="alert">{error}</p>}
          </>
        )}
        <DialogFooter>
          {state === "saved" ? (
            <Button type="button" onClick={close}>Done</Button>
          ) : (
            <Button type="button" onClick={() => void save()} disabled={state === "saving" || !text.trim()}>
              {state === "saving" ? "Saving…" : "Save"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
