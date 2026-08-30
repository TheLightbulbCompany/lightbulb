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

export type ProvisioningWorkflow = {
  workflow_id: string;
  state: string;
  provider?: string | null;
  service?: string | null;
};

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
  const providers = [...new Set([...byProvider.keys(), ...workflowsByProvider.keys()])].sort();

  return {
    byProvider,
    workflowsByProvider,
    // A failed read leaves both maps empty, which is indistinguishable from
    // "this owner has provisioned nothing" — so the caller says which.
    error: resources.error ?? workflows.error ?? null,
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
        <p key={w.workflow_id} className="text-xs text-[var(--midnight-muted)] mt-0.5">
          {w.service ? `${w.service} — ` : ""}
          {STATE_COPY[w.state]}
        </p>
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
