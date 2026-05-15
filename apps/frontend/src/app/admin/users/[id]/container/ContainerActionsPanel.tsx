"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { ConfirmActionDialog } from "@/components/admin/ConfirmActionDialog";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  queueImageUpdate,
  reprovisionContainer,
  resizeContainer,
  startContainer,
  stopContainer,
} from "@/app/admin/_actions/container";

export interface ContainerActionsPanelProps {
  userId: string;
}

/**
 * Client island for the per-user container lifecycle actions. All four
 * actions use the user_id as the typed-confirm phrase (CEO S5).
 *
 * Note on Resize: post-flat-fee (2026-04 pivot) every user runs the same
 * 0.5 vCPU / 1 GB box, so resize is now a "re-apply standard sizing"
 * recovery action — it forces the ECS service to roll its task definition
 * with the canonical per-user resource profile. The backend's resize
 * endpoint accepts and ignores the legacy `tier` argument.
 */
export function ContainerActionsPanel({ userId }: ContainerActionsPanelProps) {
  const router = useRouter();
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  const [imageUrl, setImageUrl] = React.useState("");
  const [imageDescription, setImageDescription] = React.useState("");
  const [queueing, setQueueing] = React.useState(false);

  function clearStatus() {
    setError(null);
    setNotice(null);
  }

  function applyResult(label: string, ok: boolean, errMsg?: string) {
    if (ok) {
      setNotice(`${label} succeeded.`);
      router.refresh();
    } else {
      setError(`${label} failed: ${errMsg ?? "unknown_error"}`);
    }
  }

  // Handlers are plain async functions — React Compiler memoizes them. Manual
  // useCallback would require listing applyResult/clearStatus in deps which
  // tangles with the linter's "preserve manual memoization" rule.
  async function handleReprovision() {
    clearStatus();
    const result = await reprovisionContainer(userId);
    applyResult("Reprovision", result.ok, result.error);
  }

  async function handleStop() {
    clearStatus();
    const result = await stopContainer(userId);
    applyResult("Stop", result.ok, result.error);
  }

  async function handleStart() {
    clearStatus();
    const result = await startContainer(userId);
    applyResult("Start", result.ok, result.error);
  }

  async function handleResize() {
    clearStatus();
    const result = await resizeContainer(userId);
    applyResult("Re-apply standard sizing", result.ok, result.error);
  }

  async function handleQueueImageUpdate() {
    clearStatus();
    const trimmedUrl = imageUrl.trim();
    if (!trimmedUrl) {
      setError("Image URL is required.");
      return;
    }
    setQueueing(true);
    try {
      const result = await queueImageUpdate(
        userId,
        trimmedUrl,
        imageDescription.trim() || undefined,
      );
      applyResult("Queue image update", result.ok, result.error);
      if (result.ok) {
        setImageUrl("");
        setImageDescription("");
      }
    } finally {
      setQueueing(false);
    }
  }

  return (
    <div className="space-y-3 rounded-md border border-white/10 bg-white/[0.02] p-4">
      <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-400">
        Container actions
      </h2>
      {error ? <ErrorBanner error={error} variant="error" /> : null}
      {notice ? <ErrorBanner error={notice} variant="info" /> : null}

      {/* Lifecycle */}
      <div className="flex flex-wrap gap-2">
        <ConfirmActionDialog
          confirmText={userId}
          actionLabel="Reprovision container"
          destructive
          onConfirm={handleReprovision}
        >
          <Button type="button" variant="destructive" size="sm">
            Reprovision
          </Button>
        </ConfirmActionDialog>
        <ConfirmActionDialog
          confirmText={userId}
          actionLabel="Stop container"
          destructive
          onConfirm={handleStop}
        >
          <Button type="button" variant="destructive" size="sm">
            Stop
          </Button>
        </ConfirmActionDialog>
        <ConfirmActionDialog
          confirmText={userId}
          actionLabel="Start container"
          onConfirm={handleStart}
        >
          <Button type="button" variant="outline" size="sm">
            Start
          </Button>
        </ConfirmActionDialog>
      </div>

      {/* Re-apply standard sizing (post-flat-fee: single per-user task profile) */}
      <div className="space-y-2 rounded-md border border-white/5 bg-white/[0.01] p-3">
        <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
          Re-apply standard sizing
        </h3>
        <p className="text-xs text-zinc-400">
          Forces the ECS service to roll its task definition with the
          canonical 0.5 vCPU / 1 GB profile. Use as a recovery action when
          a container is wedged on a stale task def.
        </p>
        <ConfirmActionDialog
          confirmText={userId}
          actionLabel="Re-apply standard sizing"
          onConfirm={handleResize}
        >
          <Button type="button" variant="default" size="sm">
            Re-apply sizing
          </Button>
        </ConfirmActionDialog>
      </div>

      {/* Track 2 image update (banner + user-driven apply). Non-destructive
          alternative to Reprovision — container keeps running on the old image
          until the owner clicks "Update now" in their chat banner. */}
      <div className="space-y-2 rounded-md border border-white/5 bg-white/[0.01] p-3">
        <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
          Queue image update
        </h3>
        <p className="text-xs text-zinc-400">
          Creates a pending Track 2 image update for this owner. They&apos;ll see a
          banner in chat and choose when to apply (Update now / Tonight / Remind
          later). The container keeps running on the current image until they
          accept. For the current dev image URL, see{" "}
          <code className="font-mono text-zinc-300">openclaw-version.json</code>{" "}
          on main.
        </p>
        <Input
          type="text"
          placeholder="877352799272.dkr.ecr.us-east-1.amazonaws.com/isol8/openclaw-extended:2026.5.12-570e197"
          value={imageUrl}
          onChange={(e) => setImageUrl(e.target.value)}
          disabled={queueing}
          aria-label="New image URL"
          className="font-mono text-xs"
        />
        <Textarea
          placeholder="(optional) Short description shown in the owner's update banner"
          value={imageDescription}
          onChange={(e) => setImageDescription(e.target.value)}
          disabled={queueing}
          aria-label="Update description"
          rows={2}
          className="text-xs"
        />
        <Button
          type="button"
          variant="default"
          size="sm"
          onClick={handleQueueImageUpdate}
          disabled={queueing || imageUrl.trim().length === 0}
        >
          {queueing ? "Queueing…" : "Queue image update"}
        </Button>
      </div>
    </div>
  );
}
