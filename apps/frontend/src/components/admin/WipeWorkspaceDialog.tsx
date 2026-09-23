"use client";

import * as React from "react";

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  executeWorkspaceWipe,
  previewWorkspaceWipe,
  type WipeStep,
  type WorkspaceWipeMember,
} from "@/app/admin/_actions/wipe";

import { ConfirmInput, StepList } from "./WipeAccountDialog";
import { ErrorBanner } from "./ErrorBanner";

/** Mirrors `workspace_wipe.DEFAULT_LAYERS`; billing is a separate typed opt-in. */
const LAYERS = ["runtime", "paperclip", "messaging", "secrets", "identity"] as const;
const BILLING_PHRASE = "DELETE-BILLING";
// The backend runs billing only when the layer is selected AND include_billing.
const withBilling = (layers: string[], includeBilling: boolean) =>
  includeBilling ? [...layers, "billing"] : layers;
const labelClass = "text-xs font-medium uppercase tracking-caps text-muted-foreground";

/**
 * Org-keyed workspace teardown. Same three phases as `<WipeAccountDialog>`
 * (preview on open, typed confirm, per-step result), plus option controls:
 * every option change re-previews, and execute sends the LATEST preview's
 * fingerprint so the backend refuses (409 `stale_preview`) if the workspace
 * or the options drifted. The typed org-id match is UX only — the backend
 * re-verifies `confirm_org`.
 */
export function WipeWorkspaceDialog({ orgId }: { orgId: string }) {
  const [open, setOpen] = React.useState(false);
  const [layers, setLayers] = React.useState<string[]>([...LAYERS]);
  const [includeBilling, setIncludeBilling] = React.useState(false);
  const [deleteSharedMembers, setDeleteSharedMembers] = React.useState(false);
  const [previewLoading, setPreviewLoading] = React.useState(false);
  const [previewError, setPreviewError] = React.useState<string | null>(null);
  const [previewSteps, setPreviewSteps] = React.useState<WipeStep[] | null>(null);
  const [members, setMembers] = React.useState<WorkspaceWipeMember[]>([]);
  const [fingerprint, setFingerprint] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  const [typedOrg, setTypedOrg] = React.useState("");
  const [typedBilling, setTypedBilling] = React.useState("");
  const [executing, setExecuting] = React.useState(false);
  const [resultSteps, setResultSteps] = React.useState<WipeStep[] | null>(null);
  const [executeError, setExecuteError] = React.useState<string | null>(null);
  // Only the newest preview may set the fingerprint: an older response
  // landing late would bind execute to options the admin already changed.
  const requestSeq = React.useRef(0);

  const matches = typedOrg === orgId && (!includeBilling || typedBilling === BILLING_PHRASE);
  const wipeDisabled =
    !matches || executing || previewLoading || fingerprint === null || resultSteps !== null;
  const failed = resultSteps?.some((s) => s.status === "failed") ?? false;

  async function loadPreview(opts: { layers: string[]; includeBilling: boolean; deleteSharedMembers: boolean }) {
    const seq = ++requestSeq.current;
    setPreviewLoading(true);
    setPreviewError(null);
    setFingerprint(null);
    try {
      const result = await previewWorkspaceWipe(orgId, {
        ...opts,
        layers: withBilling(opts.layers, opts.includeBilling),
      });
      if (seq !== requestSeq.current) return;
      if (!result.ok) {
        setPreviewError(result.error ?? "preview_failed");
        return;
      }
      const report = result.data as
        | { steps?: WipeStep[]; members?: WorkspaceWipeMember[]; fingerprint?: string }
        | undefined;
      setPreviewSteps(report?.steps ?? []);
      setMembers(report?.members ?? []);
      setFingerprint(report?.fingerprint ?? null);
    } finally {
      if (seq === requestSeq.current) setPreviewLoading(false);
    }
  }

  function change(next: Partial<{ layers: string[]; includeBilling: boolean; deleteSharedMembers: boolean }>) {
    const opts = { layers, includeBilling, deleteSharedMembers, ...next };
    setLayers(opts.layers);
    setIncludeBilling(opts.includeBilling);
    setDeleteSharedMembers(opts.deleteSharedMembers);
    setNotice(null);
    void loadPreview(opts);
  }

  function handleOpenChange(next: boolean) {
    if (executing) return;
    setOpen(next);
    if (next) {
      const opts = { layers: [...LAYERS], includeBilling: false, deleteSharedMembers: false };
      setLayers(opts.layers);
      setIncludeBilling(false);
      setDeleteSharedMembers(false);
      setPreviewSteps(null);
      setMembers([]);
      setNotice(null);
      setTypedOrg("");
      setTypedBilling("");
      setResultSteps(null);
      setExecuteError(null);
      void loadPreview(opts);
    }
  }

  async function handleWipe(event: React.MouseEvent<HTMLButtonElement>) {
    event.preventDefault();
    if (wipeDisabled || fingerprint === null) return;
    setExecuting(true);
    setExecuteError(null);
    setNotice(null);
    try {
      const result = await executeWorkspaceWipe(orgId, {
        confirmOrg: typedOrg,
        previewFingerprint: fingerprint,
        layers: withBilling(layers, includeBilling),
        includeBilling,
        confirmBilling: includeBilling ? typedBilling : undefined,
        deleteSharedMembers,
      });
      if (!result.ok) {
        if (result.code === "stale_preview") {
          setNotice("Workspace changed since preview — re-previewed. Review and confirm again.");
          void loadPreview({ layers, includeBilling, deleteSharedMembers });
          return;
        }
        setExecuteError(result.error ?? "wipe_failed");
        return;
      }
      const report = result.data as { steps?: WipeStep[] } | undefined;
      setResultSteps(report?.steps ?? []);
    } finally {
      setExecuting(false);
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={handleOpenChange}>
      <AlertDialogTrigger asChild>
        <Button type="button" variant="destructive" size="sm">
          Wipe workspace
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent className="max-w-xl">
        <AlertDialogHeader>
          <AlertDialogTitle>Wipe workspace</AlertDialogTitle>
          <AlertDialogDescription>
            Tears down the selected layers for workspace{" "}
            <span className="font-mono">{orgId}</span> and all its members. Billing is
            preserved unless explicitly included. This cannot be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>

        {resultSteps ? (
          <div className="space-y-2">
            <h3 className={labelClass}>Result</h3>
            {failed ? (
              <ErrorBanner
                variant="error"
                error="Partial wipe: some steps failed. Re-run to finish: close, reopen, and wipe again."
              />
            ) : null}
            <StepList steps={resultSteps} />
          </div>
        ) : (
          <div className="max-h-[60vh] space-y-4 overflow-y-auto">
            <fieldset className="space-y-1" disabled={executing}>
              <legend className={labelClass}>Layers</legend>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
                {LAYERS.map((layer) => (
                  <label key={layer} className="flex items-center gap-1.5">
                    <input
                      type="checkbox"
                      checked={layers.includes(layer)}
                      onChange={(e) =>
                        change({
                          layers: e.target.checked ? [...layers, layer] : layers.filter((l) => l !== layer),
                        })
                      }
                    />
                    {layer}
                  </label>
                ))}
              </div>
              <label className="flex items-center gap-1.5 text-sm">
                <input
                  type="checkbox"
                  checked={deleteSharedMembers}
                  onChange={(e) => change({ deleteSharedMembers: e.target.checked })}
                />
                Also delete members who belong to other workspaces
              </label>
              <label className="flex items-center gap-1.5 text-sm">
                <input
                  type="checkbox"
                  checked={includeBilling}
                  onChange={(e) => change({ includeBilling: e.target.checked })}
                />
                Include billing (Stripe customer, Metronome, billing row)
              </label>
            </fieldset>

            {notice ? <ErrorBanner variant="info" error={notice} /> : null}

            {previewLoading ? (
              <p className="text-sm text-muted-foreground">Loading preview…</p>
            ) : previewError ? (
              <ErrorBanner error={previewError} variant="error" />
            ) : previewSteps ? (
              <>
                <div className="space-y-2">
                  <h3 className={labelClass}>Members</h3>
                  {members.length === 0 ? (
                    <p className="text-sm text-muted-foreground">No members found.</p>
                  ) : (
                    <ul className="space-y-1 rounded-md border border-border p-2 text-xs">
                      {members.map((m) => (
                        <li key={m.user_id} className="flex justify-between gap-3 px-2 py-1">
                          <span className="min-w-0 break-all font-mono text-foreground">{m.email || m.user_id}</span>
                          <span className="shrink-0 text-muted-foreground">
                            {m.action === "delete_account"
                              ? "delete account"
                              : `remove membership (also in ${m.other_orgs.join(", ")})`}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <div className="space-y-2">
                  <h3 className={labelClass}>Will delete</h3>
                  <StepList steps={previewSteps} />
                </div>
              </>
            ) : null}

            {includeBilling ? (
              <ConfirmInput
                id="wipe-confirm-billing"
                phrase={BILLING_PHRASE}
                action="to delete billing"
                value={typedBilling}
                onChange={setTypedBilling}
                disabled={executing}
              />
            ) : null}
            <ConfirmInput
              id="wipe-confirm-org"
              phrase={orgId}
              action="to confirm"
              value={typedOrg}
              onChange={setTypedOrg}
              disabled={executing}
            />
          </div>
        )}

        {executeError ? <ErrorBanner error={executeError} variant="error" /> : null}

        <AlertDialogFooter>
          <AlertDialogCancel disabled={executing}>{resultSteps ? "Close" : "Cancel"}</AlertDialogCancel>
          {resultSteps ? null : (
            <Button
              type="button"
              variant="destructive"
              onClick={handleWipe}
              disabled={wipeDisabled}
              aria-busy={executing}
            >
              {executing ? "Wiping…" : "Permanently wipe"}
            </Button>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
