"use client";

import { useState } from "react";
import useSWR from "swr";

import { Input } from "@/components/ui/input";
import { useApi } from "@/lib/api";
import { notifyApiError, notifyError } from "@/lib/notify";
import { closePendingTab, navigatePendingTab, openPendingTab } from "@/components/settings/openInNewTab";

/**
 * The Stripe Projects rail's facet on a Connections card.
 *
 * Two rails, one card (spec §3.2): the OAuth relationship ("Account
 * connected", owned by ConnectButton) and the resources this rail CREATED on
 * the owner's own provider identity. They collide on posthog/sentry/cloudflare
 * and diverge everywhere else, which is why the resources hang off the same
 * provider card instead of a section of their own.
 *
 * Nothing pre-authenticated is stored: the provider dashboard deep link is
 * minted for THIS click and handed straight to a tab, never rendered as an
 * href and never kept in state.
 */

export type ProvisionedResource = {
  resource_id: string;
  provider: string;
  service: string;
  status: string;
  resource_name?: string | null;
  /** Variable NAMES only — the values never leave the container. */
  env_keys: string[];
  level: string;
};

/** Names and an e-mail — `provisioning_workflow.safe_account_details` allows
 *  nothing else through, and certainly not a provider id or a token. */
export type ProviderAccount = {
  display_name?: string | null;
  primary_email?: string | null;
};

export type ProvisioningWorkflow = {
  workflow_id: string;
  state: string;
  provider?: string | null;
  service?: string | null;
  provider_account?: ProviderAccount | null;
};

/** The account line for a card: "Acme Ltd · ops@acme.com", or null. */
export function accountLabel(account: ProviderAccount | null | undefined): string | null {
  return [account?.display_name, account?.primary_email].filter(Boolean).join(" · ") || null;
}

const RESOURCES_PATH = "/company/provisioning/resources";
const WORKFLOWS_PATH = "/company/provisioning/workflows";

const POPUP_BLOCKED_MSG =
  "Your browser blocked the new tab — allow popups for this site and try again.";
const NO_DASHBOARD_MSG =
  "This provider doesn't offer a dashboard link right now — open it from the provider's own site.";
const REVOKED_MSG =
  "Disconnected from Lightbulb AI. You keep the account — remove it in the provider's own dashboard if you want it gone.";

/**
 * What an unfinished request looks like to the owner. Keyed by the workflow
 * states that are still SOMEBODY'S move — `active`, `rejected` and `cancelled`
 * are absent on purpose, so a settled request leaves no chip behind.
 */
const STATE_COPY: Record<string, string> = {
  ask: "Working out the details",
  awaiting_approval: "Waiting for your approval",
  connecting_provider: "Connecting to the provider",
  awaiting_user_action: "Waiting on you — see your inbox",
  stale: "Waiting on you — see your inbox",
  provisioning: "Setting it up",
  binding: "Handing the keys to your agents",
  failed: "Didn't work — your agents can ask again",
  requires_review: "This one needs a look from Lightbulb — email support and we'll take it from here",
};

/**
 * Which open states an ask can still simply be WITHDRAWN from. Mirrors the
 * backend's `CANCELLABLE_STATES`; the backend refuses again on the ids it can
 * see and this cannot (an account request or a resource already exists), and
 * that refusal is surfaced rather than swallowed.
 */
const WITHDRAWABLE = new Set(["ask", "awaiting_approval", "awaiting_user_action", "stale"]);

export function isOpenWorkflow(w: ProvisioningWorkflow): boolean {
  return Object.hasOwn(STATE_COPY, w.state ?? "");
}

function group<T>(rows: T[], key: (row: T) => string): Map<string, T[]> {
  const out = new Map<string, T[]>();
  for (const row of rows) {
    const k = key(row);
    if (!k) continue;
    const bucket = out.get(k);
    if (bucket) bucket.push(row);
    else out.set(k, [row]);
  }
  return out;
}

/**
 * The rail's two reads, grouped per provider for the Connections cards.
 *
 * `GET /connections` does not carry these rows, so this is one extra read —
 * the same one the standalone Accounts section used to make — plus the
 * workflow list, which any member may read.
 */
export function useProvisioningRail() {
  const api = useApi();
  const resources = useSWR<{ resources: ProvisionedResource[] }>(
    RESOURCES_PATH,
    (p: string) => api.get(p) as Promise<{ resources: ProvisionedResource[] }>,
  );
  const workflows = useSWR<{ workflows: ProvisioningWorkflow[] }>(
    WORKFLOWS_PATH,
    (p: string) => api.get(p) as Promise<{ workflows: ProvisioningWorkflow[] }>,
  );

  const byProvider = group(resources.data?.resources ?? [], (r) => r.provider);
  const workflowsByProvider = group(
    (workflows.data?.workflows ?? []).filter(isOpenWorkflow),
    (w) => w.provider ?? "",
  );
  // WHICH provider account this rail ended up creating or linking. Read off the
  // finished workflow, because that is the only place the owner is ever told —
  // the resource pointer carries names of variables, not names of accounts.
  const accountByProvider = new Map<string, ProviderAccount>();
  for (const w of workflows.data?.workflows ?? []) {
    const provider = w.provider ?? "";
    if (!provider || w.state !== "active" || !w.provider_account) continue;
    if (!accountByProvider.has(provider)) accountByProvider.set(provider, w.provider_account);
  }
  const providers = [...new Set([...byProvider.keys(), ...workflowsByProvider.keys()])].sort();

  return {
    byProvider,
    workflowsByProvider,
    accountByProvider,
    // A failed read leaves both maps empty, which is indistinguishable from
    // "this owner has provisioned nothing" — so the caller says which.
    error: resources.error ?? workflows.error ?? null,
    // Same ambiguity the comment above names, for the in-flight case: on a
    // cold cache both maps are empty with no error, so a consumer would
    // assert "nothing provisioned" before a byte has arrived.
    isLoading: resources.isLoading || workflows.isLoading,
    /** Every provider this rail knows about — the superset the card list needs. */
    providers,
    refresh: () => {
      void resources.mutate();
      void workflows.mutate();
    },
  };
}

export function ProvisionedResourcesFacet({
  provider,
  resources,
  workflows,
  canManage,
  onChanged,
}: {
  provider: string;
  resources: ProvisionedResource[];
  workflows: ProvisioningWorkflow[];
  canManage: boolean;
  onChanged: () => void;
}) {
  const [note, setNote] = useState<string | null>(null);

  if (resources.length === 0 && workflows.length === 0) return null;

  return (
    <div className="mt-2 border-t border-[var(--edge)] pt-2">
      <p className="text-xs font-medium text-[var(--midnight)]">Resources</p>
      {workflows.map((w) => (
        <OpenWorkflowLine key={w.workflow_id} workflow={w} canManage={canManage} onChanged={onChanged} />
      ))}
      {resources.map((r) => (
        <ResourceLine
          key={r.resource_id}
          resource={r}
          provider={provider}
          canManage={canManage}
          onChanged={onChanged}
          onNote={setNote}
        />
      ))}
      {note && <p className="text-xs text-[var(--midnight-muted)] mt-1">{note}</p>}
    </div>
  );
}

/**
 * One unfinished request. The owner's only way to call an ask off from
 * Settings — the Inbox card covers the ones with a card, and a row parked in
 * `ask` or gone `stale` has none.
 */
function OpenWorkflowLine({
  workflow,
  canManage,
  onChanged,
}: {
  workflow: ProvisioningWorkflow;
  canManage: boolean;
  onChanged: () => void;
}) {
  const api = useApi();
  const [busy, setBusy] = useState(false);

  async function withdraw() {
    if (busy) return;
    setBusy(true);
    try {
      await api.post(`/company/provisioning/workflows/${workflow.workflow_id}/cancel`, {});
      onChanged();
    } catch (e) {
      // 409 `cannot_cancel` is the honest answer, not a failure: something has
      // already been created and only `remove` takes it back.
      notifyApiError(e, "That request could not be withdrawn.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <p className="flex flex-wrap items-center gap-2 text-xs text-[var(--midnight-muted)] mt-0.5">
      <span>
        {workflow.service ? `${workflow.service} — ` : ""}
        {STATE_COPY[workflow.state]}
      </span>
      {canManage && WITHDRAWABLE.has(workflow.state) && (
        <button type="button" className="settings-btn-outline" disabled={busy} onClick={withdraw}>
          Withdraw
        </button>
      )}
    </p>
  );
}

function ResourceLine({
  resource,
  provider,
  canManage,
  onChanged,
  onNote,
}: {
  resource: ProvisionedResource;
  provider: string;
  canManage: boolean;
  onChanged: () => void;
  onNote: (note: string) => void;
}) {
  const api = useApi();
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState("");
  const base = `/company/provisioning/resources/${provider}/${resource.resource_id}`;
  // What the backend compares the typed confirmation against.
  const expected = resource.resource_name || resource.resource_id;

  async function act(fn: () => Promise<void>) {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      notifyApiError(e, "That didn't work. Try again.");
    } finally {
      setBusy(false);
    }
  }

  /** Minted for THIS click and handed to a tab opened synchronously (a
   *  `window.open` after an `await` is blocked in Safari). Never state, never
   *  an href. */
  function openDashboard() {
    const w = openPendingTab();
    if (!w) {
      notifyError(POPUP_BLOCKED_MSG);
      return;
    }
    void act(async () => {
      let url: string | null | undefined;
      try {
        url = ((await api.post(`${base}/dashboard-link`, {})) as { url?: string | null }).url;
      } catch (e) {
        closePendingTab(w);
        throw e;
      }
      if (!url) {
        closePendingTab(w);
        notifyError(NO_DASHBOARD_MSG);
        return;
      }
      navigatePendingTab(w, url);
    });
  }

  return (
    <div className="flex flex-col gap-1 mt-1">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-medium text-[var(--midnight)]">{expected}</span>
        <span className="text-[var(--midnight-muted)]">{resource.service}</span>
        <span className="text-[var(--midnight-muted)]">{resource.status}</span>
        {/* Spec §3.4: state the level per resource. */}
        <span className="text-[var(--midnight-muted)]">
          {resource.level === "company" ? "Company-level" : "Agent-level"}
        </span>
        {/* All three routes below are admin-and-paid (`Owner` in the
            provisioning router), so a member sees the row without them. */}
        {canManage && (
          <>
            <button type="button" className="settings-btn-outline" disabled={busy} onClick={openDashboard}>
              Open dashboard
            </button>
            <button
              type="button"
              className="settings-btn-outline"
              disabled={busy}
              onClick={() =>
                act(async () => {
                  await api.post(`${base}/revoke`, {});
                  onNote(REVOKED_MSG);
                  onChanged();
                })
              }
            >
              Revoke our copy
            </button>
            <button
              type="button"
              className="settings-btn-outline"
              disabled={busy}
              onClick={() => setConfirming(true)}
            >
              Remove
            </button>
          </>
        )}
      </div>
      {confirming && (
        <div className="flex flex-col gap-1 text-xs">
          <p className="text-[var(--err)]">
            This deletes it at {provider} and stops any charges for it. Your agents lose the
            credential. It cannot be undone.
          </p>
          <label className="text-[var(--midnight-muted)]" htmlFor={`confirm-${resource.resource_id}`}>
            Type the name to confirm: {expected}
          </label>
          <Input
            id={`confirm-${resource.resource_id}`}
            type="text"
            autoComplete="off"
            spellCheck={false}
            className="h-9 bg-cream"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
          />
          <div className="flex gap-2">
            <button
              type="button"
              className="settings-btn-outline"
              disabled={busy || typed.trim() !== expected}
              onClick={() =>
                act(async () => {
                  await api.post(`${base}/remove`, { confirm: typed.trim() });
                  setConfirming(false);
                  setTyped("");
                  onNote(`${expected} was removed at ${provider}.`);
                  onChanged();
                })
              }
            >
              Remove for good
            </button>
            <button
              type="button"
              className="settings-btn-outline"
              disabled={busy}
              onClick={() => {
                setConfirming(false);
                setTyped("");
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
