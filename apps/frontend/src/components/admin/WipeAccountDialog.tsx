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
import { Input } from "@/components/ui/input";
import {
  executeAccountWipe,
  previewAccountWipe,
  type WorkspaceMembership,
  type WipeStep,
} from "@/app/admin/_actions/wipe";

import { ErrorBanner } from "./ErrorBanner";
import { StatusChip } from "./StatusChip";

export interface WipeAccountDialogProps {
  userId: string;
  /**
   * Primary email for the account — required so the typed-confirm gate has
   * something to compare against. The backend re-verifies the same match
   * server-side against its own record, so this prop is UX only, never the
   * safety boundary.
   */
  email: string | null;
}

/**
 * Full-account teardown. Deliberately its own component rather than
 * `<ConfirmActionDialog>` — that primitive models a single typed-phrase
 * confirm, but this flow has three phases in one dialog: fetch + render a
 * dry-run preview on open, gate a destructive action behind an exact email
 * match (not just "any text", per the CEO S5 convention used elsewhere —
 * this is the platform's single most destructive action), then render the
 * per-step execute report in place. Structurally mirrors
 * `TakedownButton` (rich `AlertDialog` body, plain `Button` for the confirm
 * action instead of `AlertDialogAction` so async work can run before the
 * dialog closes).
 */
export function WipeAccountDialog({ userId, email }: WipeAccountDialogProps) {
  const [open, setOpen] = React.useState(false);
  const [previewLoading, setPreviewLoading] = React.useState(false);
  const [previewSteps, setPreviewSteps] = React.useState<WipeStep[] | null>(null);
  const [previewError, setPreviewError] = React.useState<string | null>(null);
  const [typedEmail, setTypedEmail] = React.useState("");
  // Every workspace the account belongs to: their companies are kept, and the
  // backend demands the first workspace's name.
  const [workspaces, setWorkspaces] = React.useState<WorkspaceMembership[]>([]);
  const [typedWorkspace, setTypedWorkspace] = React.useState("");
  const [typedTeardown, setTypedTeardown] = React.useState<Record<string, string>>({});
  const [executing, setExecuting] = React.useState(false);
  const [resultSteps, setResultSteps] = React.useState<WipeStep[] | null>(null);
  const [executeError, setExecuteError] = React.useState<string | null>(null);

  const workspacePhrase = workspaces.length ? workspaces[0].workspace_name || workspaces[0].org_id : null;
  // Removing a workspace's LAST member tears that workspace down (#3115): each one is
  // named a second time, on its own, before the wipe can run.
  const teardowns = workspaces.filter((w) => w.last_member);
  const teardownPhrase = (w: WorkspaceMembership) => w.workspace_name || w.org_id;
  const matches =
    email != null &&
    typedEmail === email &&
    (workspacePhrase === null || typedWorkspace === workspacePhrase) &&
    teardowns.every((w) => typedTeardown[w.org_id] === teardownPhrase(w));
  const wipeDisabled = !matches || executing || previewSteps === null || resultSteps !== null;

  async function loadPreview() {
    setPreviewLoading(true);
    setPreviewError(null);
    setPreviewSteps(null);
    try {
      const result = await previewAccountWipe(userId);
      if (!result.ok) {
        setPreviewError(result.error ?? "preview_failed");
        return;
      }
      const report = result.data as
        | { steps?: WipeStep[]; workspaces?: WorkspaceMembership[] }
        | undefined;
      setWorkspaces(report?.workspaces ?? []);
      setPreviewSteps(report?.steps ?? []);
    } finally {
      setPreviewLoading(false);
    }
  }

  function handleOpenChange(next: boolean) {
    if (executing) return;
    setOpen(next);
    if (next) {
      // Reset any stale state from a previous open before fetching fresh.
      setPreviewError(null);
      setPreviewSteps(null);
      setTypedEmail("");
      setWorkspaces([]);
      setTypedWorkspace("");
      setTypedTeardown({});
      setResultSteps(null);
      setExecuteError(null);
      void loadPreview();
    }
  }

  async function handleWipe(event: React.MouseEvent<HTMLButtonElement>) {
    event.preventDefault();
    if (wipeDisabled) return;
    setExecuting(true);
    setExecuteError(null);
    try {
      const result = await executeAccountWipe(
        userId,
        typedEmail,
        workspacePhrase === null ? undefined : typedWorkspace,
        teardowns.map((w) => typedTeardown[w.org_id] ?? ""),
      );
      if (!result.ok) {
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
        <Button
          type="button"
          variant="destructive"
          size="sm"
          disabled={!email}
          title={email ? undefined : "No primary email on file — wipe is disabled until Clerk reports one."}
        >
          Wipe account
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent className="max-w-xl">
        <AlertDialogHeader>
          <AlertDialogTitle>Wipe account</AlertDialogTitle>
          <AlertDialogDescription>
            Permanently tears down every resource for this owner — runtime
            and S3 profiles, Paperclip company, Bedrock credits/role,
            billing, and the Clerk user itself. This cannot be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>

        {previewLoading ? (
          <p className="text-sm text-muted-foreground">Loading preview…</p>
        ) : previewError ? (
          <ErrorBanner error={previewError} variant="error" />
        ) : resultSteps ? (
          <div className="space-y-2">
            <h3 className="text-xs font-medium uppercase tracking-caps text-muted-foreground">
              Result
            </h3>
            <StepList steps={resultSteps} />
          </div>
        ) : previewSteps ? (
          <div className="space-y-4">
            {workspaces.length ? (
              <ErrorBanner variant="info" error={workspacesNotice(workspaces)} />
            ) : null}
            <div className="space-y-2">
              <h3 className="text-xs font-medium uppercase tracking-caps text-muted-foreground">
                Will delete
              </h3>
              <StepList steps={previewSteps} />
            </div>
            <ConfirmInput
              id="wipe-confirm-email"
              phrase={email ?? ""}
              action="to confirm"
              value={typedEmail}
              onChange={setTypedEmail}
              disabled={executing}
            />
            {workspacePhrase ? (
              <ConfirmInput
                id="wipe-confirm-workspace"
                phrase={workspacePhrase}
                action="to confirm the workspace"
                value={typedWorkspace}
                onChange={setTypedWorkspace}
                disabled={executing}
              />
            ) : null}
            {teardowns.map((w) => (
              <div key={w.org_id} className="space-y-2">
                <ErrorBanner
                  variant="error"
                  error={
                    `This account is the last member of ${w.workspace_name ? `"${w.workspace_name}"` : w.org_id}. ` +
                    `Removing it tears that workspace down: it ${(w.teardown ?? []).join("; ")}.`
                  }
                />
                <ConfirmInput
                  id={`wipe-confirm-teardown-${w.org_id}`}
                  phrase={teardownPhrase(w)}
                  action="to tear that workspace down"
                  value={typedTeardown[w.org_id] ?? ""}
                  onChange={(value) => setTypedTeardown((prev) => ({ ...prev, [w.org_id]: value }))}
                  disabled={executing}
                />
              </div>
            ))}
          </div>
        ) : null}

        {executeError ? <ErrorBanner error={executeError} variant="error" /> : null}

        <AlertDialogFooter>
          <AlertDialogCancel disabled={executing}>
            {resultSteps ? "Close" : "Cancel"}
          </AlertDialogCancel>
          {resultSteps || previewError ? null : (
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

function workspacesNotice(workspaces: WorkspaceMembership[]): string {
  const described = workspaces.map((w) => {
    const workspace = w.workspace_name ? `workspace "${w.workspace_name}"` : "a workspace";
    const owner = w.owner_user_id ?? "unknown";
    const ownerEmail = w.owner_email ? ` <${w.owner_email}>` : "";
    const members = `${w.member_count} active member${w.member_count === 1 ? "" : "s"}`;
    return `${workspace} (${w.org_id}, owner ${owner}${ownerEmail}, ${members})`;
  });
  return (
    `This account is a member of ${described.join("; ")}. ` +
    "Every workspace's Paperclip company will be kept; only this member's membership is archived."
  );
}

/** "Type <phrase> to …" confirmation field shared by the wipe dialogs. */
export function ConfirmInput({
  id,
  phrase,
  action,
  value,
  onChange,
  disabled,
}: {
  id: string;
  phrase: string;
  action: string;
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="text-xs font-medium uppercase tracking-caps text-muted-foreground">
        Type <span className="font-mono font-semibold text-foreground">{phrase}</span> {action}
      </label>
      <Input
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={phrase}
        autoComplete="off"
        spellCheck={false}
        disabled={disabled}
        aria-label={`Type ${phrase} ${action}`}
      />
    </div>
  );
}

export function StepList({ steps }: { steps: WipeStep[] }) {
  if (steps.length === 0) {
    return <p className="text-sm text-muted-foreground">No resources found for this owner.</p>;
  }
  return (
    <ul className="max-h-72 space-y-1 overflow-y-auto rounded-md border border-border p-2">
      {steps.map((step, i) => (
        <li
          key={`${step.system}-${step.action}-${i}`}
          className="flex items-start justify-between gap-3 rounded-sm px-2 py-1.5 text-xs"
        >
          <div className="min-w-0 break-all">
            <div className="font-mono text-foreground">
              <span>{step.system}</span>
              <span className="mx-1 text-muted-foreground">·</span>
              <span>{step.action}</span>
            </div>
            {step.detail ? (
              <div className="text-muted-foreground">
                {step.detail}
              </div>
            ) : null}
            {step.error ? (
              <div role="alert" className="text-err">
                {step.error}
              </div>
            ) : null}
          </div>
          <StatusChip status={step.status} />
        </li>
      ))}
    </ul>
  );
}
