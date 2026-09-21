"use client";

import { useCallback, useMemo, useState, useSyncExternalStore, type ReactElement } from "react";
import { Check, Users, Pencil, Plus, X } from "lucide-react";
import { useOrganization } from "@clerk/nextjs";

import { useApi } from "@/lib/api";
import { useConnections } from "@/hooks/useConnections";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  buildSetupSteps,
  isBandComplete,
  STARTER_AGENT_DEFAULT_NAMES,
  type SetupStep,
} from "./onboardingSteps";
import { openPanel } from "@/components/workspace/workspaceController";
import { ConnectorIcon } from "./ConnectorIcons";
import type { RoomParticipant } from "./types";
import type { JoinLinkStatus } from "@/components/settings/WorkspaceMembersSection";

/** `POST /orgs/{id}/join-link` — the freshly minted link. */
type CreatedJoinLink = { join_url: string };

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

const DISMISS_KEY = "lightbulb.messaging.onboarding-band.dismissed";

/** Why a card is inert, in the words of the person looking at it. */
const NON_ADMIN_HINT = "Ask an admin";

function readDismissed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(DISMISS_KEY) === "1";
  } catch {
    return false;
  }
}

function writeDismissed(): void {
  try {
    window.localStorage.setItem(DISMISS_KEY, "1");
  } catch {
    // A viewer with storage blocked simply sees the band again next session.
  }
}

interface OnboardingBandProps {
  participants: readonly RoomParticipant[];
}

export function OnboardingBand({ participants }: OnboardingBandProps): ReactElement | null {
  const { connections, startOAuth } = useConnections();
  const { organization, membership } = useOrganization();
  const teams = useTeamsApi();
  // Same SWR key MessagingWorkspaceContext already reads, so this shares its
  // cache entry rather than issuing a second request.
  const { data: roster, mutate: refreshRoster } = teams.read<{ agents?: CompanyAgent[] }>("/agents");
  const api = useApi();

  // localStorage read through useSyncExternalStore rather than an effect:
  // `react-hooks/set-state-in-effect` is an ERROR in this repo, and the server
  // snapshot (`false`) keeps hydration honest instead of producing a mismatch.
  // Nothing outside this component writes the key, so `subscribe` is a no-op
  // and the in-session dismissal below drives the re-render.
  const subscribe = useCallback(() => () => {}, []);
  const storedDismissal = useSyncExternalStore(subscribe, readDismissed, () => false);
  const [dismissedNow, setDismissedNow] = useState(false);

  const [naming, setNaming] = useState(false);
  const [invite, setInvite] = useState<"idle" | "working" | "copied" | "failed">("idle");

  const agentNames = useMemo(
    () => new Map((roster?.agents ?? []).map((agent) => [agent.id, agent.name] as const)),
    [roster],
  );
  const steps = useMemo(
    () => buildSetupSteps({ participants, connections, agentNames }),
    [participants, connections, agentNames],
  );

  const isAdmin = membership?.role === "org:admin";

  const copyJoinLink = useCallback(async () => {
    if (!organization) return;
    setInvite("working");
    try {
      const path = `/orgs/${encodeURIComponent(organization.id)}/join-link`;
      // A workspace that has never shared a link has none to read; minting one
      // is the same action from the owner's point of view, so do it silently
      // rather than making them click twice.
      const existing = (await api.get(path)) as JoinLinkStatus;
      const url = existing?.exists && existing.join_url
        ? existing.join_url
        : ((await api.post(path, {})) as CreatedJoinLink).join_url;
      await navigator.clipboard.writeText(url);
      setInvite("copied");
    } catch {
      setInvite("failed");
    }
  }, [api, organization]);

  const onStep = useCallback((step: SetupStep) => {
    if (step.id === "invite") return void copyJoinLink();
    if (step.id === "name-agents") return setNaming((open) => !open);
    void startOAuth(step.id);
  }, [copyJoinLink, startOAuth]);

  if (storedDismissal || dismissedNow || isBandComplete(steps)) return null;

  const connectors = steps.filter((step) => step.id !== "invite" && step.id !== "name-agents");
  const actions = steps.filter((step) => step.id === "invite" || step.id === "name-agents");

  return (
    <section className="onboarding-band" aria-label="Set up your workspace">
      <header className="onboarding-band-head">
        <div>
          <h3>Get started</h3>
          <p>Connect what your team already uses, then bring the rest of them in.</p>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="onboarding-band-dismiss"
          aria-label="Dismiss setup"
          onClick={() => {
            writeDismissed();
            setDismissedNow(true);
          }}
        >
          <X size={16} aria-hidden="true" />
        </Button>
      </header>

      <div className="onboarding-band-grid">
        {connectors.map((step) => (
          <StepCard
            key={step.id}
            step={step}
            isAdmin={isAdmin}
            icon={<ConnectorIcon provider={step.id} />}
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
          onClick={() => openPanel("connections", {}, { pushUrl: true })}
        >
          <Plus size={26} aria-hidden="true" strokeWidth={1.75} />
          <span className="onboarding-card-label">See more connections</span>
        </button>
      </div>

      <div className="onboarding-band-actions">
        {actions.map((step) => (
          <StepCard
            key={step.id}
            step={step}
            isAdmin={isAdmin}
            wide
            icon={step.id === "invite" ? <Users size={16} aria-hidden="true" /> : <Pencil size={16} aria-hidden="true" />}
            status={step.id === "invite" ? inviteStatus(invite) : undefined}
            onSelect={onStep}
          />
        ))}
      </div>

      {naming && (
        <AgentNames
          participants={participants}
          agentNames={agentNames}
          onSaved={async () => {
            setNaming(false);
            await refreshRoster();
          }}
        />
      )}
    </section>
  );
}

function inviteStatus(state: "idle" | "working" | "copied" | "failed"): string | undefined {
  if (state === "working") return "Copying…";
  if (state === "copied") return "Link copied";
  if (state === "failed") return "Couldn’t copy — try again";
  return undefined;
}

function StepCard({ step, isAdmin, onSelect, wide, icon, status }: {
  step: SetupStep;
  isAdmin: boolean;
  onSelect: (step: SetupStep) => void;
  wide?: boolean;
  icon?: ReactElement;
  status?: string;
}): ReactElement {
  const blocked = step.adminOnly && !isAdmin;
  // A done card stays on screen until the whole band goes, so the row does not
  // reflow under the cursor as each one lands.
  const detail = step.done ? "Connected" : blocked ? NON_ADMIN_HINT : status ?? step.detail;
  return (
    <button
      type="button"
      className="onboarding-card"
      data-wide={wide ? "true" : undefined}
      data-done={step.done ? "true" : undefined}
      disabled={step.done || blocked}
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
function AgentNames({ participants, agentNames, onSaved }: {
  participants: readonly RoomParticipant[];
  agentNames: ReadonlyMap<string, string>;
  onSaved: () => void | Promise<void>;
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

  const save = async () => {
    setSaving(true);
    try {
      const changed = agents.filter((agent) => {
        const next = draft[agent.id]?.trim();
        return next && next !== agent.name;
      });
      await Promise.all(changed.map((agent) =>
        api.patch(`/teams/agents/${encodeURIComponent(agent.id)}`, { name: draft[agent.id].trim() }),
      ));
      await onSaved();
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
            <label htmlFor={`onboarding-name-${agent.id}`}>
              {STARTER_AGENT_DEFAULT_NAMES.has(agent.name) ? agent.name : `${agent.name}`}
            </label>
            <Input
              id={`onboarding-name-${agent.id}`}
              defaultValue={agent.name}
              maxLength={80}
              autoComplete="off"
              onChange={(event) => setDraft((current) => ({ ...current, [agent.id]: event.target.value }))}
            />
          </li>
        ))}
      </ul>
      <Button type="button" onClick={() => void save()} disabled={saving}>
        {saving ? "Saving…" : "Save names"}
      </Button>
    </div>
  );
}
