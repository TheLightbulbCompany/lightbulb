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
  type SharedCompany,
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
  // Set when the account is a workspace member: its Paperclip company is the
  // whole workspace's, so the backend keeps it and demands the workspace name.
  const [shared, setShared] = React.useState<SharedCompany | null>(null);
  const [typedWorkspace, setTypedWorkspace] = React.useState("");
  const [executing, setExecuting] = React.useState(false);
  const [resultSteps, setResultSteps] = React.useState<WipeStep[] | null>(null);
  const [executeError, setExecuteError] = React.useState<string | null>(null);

  const workspacePhrase = shared ? shared.workspace_name || shared.org_id : null;
  const matches =
    email != null &&
    typedEmail === email &&
    (workspacePhrase === null || typedWorkspace === workspacePhrase);
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
        | { steps?: WipeStep[]; shared_company?: SharedCompany | null }
        | undefined;
      setShared(report?.shared_company ?? null);
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
      setShared(null);
      setTypedWorkspace("");
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
            {shared ? (
              <ErrorBanner variant="info" error={sharedCompanyNotice(shared)} />
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

function sharedCompanyNotice(shared: SharedCompany): string {
  const workspace = shared.workspace_name
    ? `workspace "${shared.workspace_name}"`
    : "a shared workspace";
  const owner = shared.owner_user_id ?? "unknown";
  const ownerEmail = shared.owner_email ? ` <${shared.owner_email}>` : "";
  const members = `${shared.member_count} active member${shared.member_count === 1 ? "" : "s"}`;
  return (
    `This account is a member of ${workspace} (${shared.org_id}, owner ${owner}${ownerEmail}, ${members}). ` +
    "The workspace's Paperclip company will be kept; only this member's membership is archived."
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
