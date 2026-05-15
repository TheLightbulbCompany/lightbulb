"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { ConfirmActionDialog } from "@/components/admin/ConfirmActionDialog";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { AvailableImage } from "@/app/admin/_lib/api";
import {
  queueImageUpdate,
  reprovisionContainer,
  resizeContainer,
  startContainer,
  stopContainer,
} from "@/app/admin/_actions/container";

const CUSTOM_IMAGE_VALUE = "__custom__";

export interface ContainerActionsPanelProps {
  userId: string;
  /**
   * Recently-pushed ECR tags from /admin/container/available-images, newest
   * first. Empty when ECR is unreachable — the UI then falls back to a plain
   * free-text input so the admin isn't blocked.
   */
  availableImages?: AvailableImage[];
  /** Tag pinned as dev in openclaw-version.json — rendered as a badge on the matching row. */
  currentDevTag?: string;
  /** Tag pinned as prod in openclaw-version.json — rendered as a badge on the matching row. */
  currentProdTag?: string;
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
export function ContainerActionsPanel({
  userId,
  availableImages,
  currentDevTag,
  currentProdTag,
}: ContainerActionsPanelProps) {
  const router = useRouter();
  const images = availableImages ?? [];
  const hasImages = images.length > 0;
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  // Default to the most-recently-pushed image when ECR returned tags; the
  // first row is the freshest build, which is the common case for the
  // "bump this user to the new dev image" workflow. Falls back to "custom"
  // when ECR errored out so the free-text field gets a useful starting state.
  const [selectedUri, setSelectedUri] = React.useState<string>(
    hasImages ? images[0].image_uri : CUSTOM_IMAGE_VALUE,
  );
  const [customImageUrl, setCustomImageUrl] = React.useState("");
  const [imageDescription, setImageDescription] = React.useState("");
  const [queueing, setQueueing] = React.useState(false);

  const usingCustom = selectedUri === CUSTOM_IMAGE_VALUE;
  const effectiveImageUrl = usingCustom ? customImageUrl : selectedUri;

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
    const trimmedUrl = effectiveImageUrl.trim();
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
        setCustomImageUrl("");
        setImageDescription("");
        if (hasImages) setSelectedUri(images[0].image_uri);
      }
    } finally {
      setQueueing(false);
    }
  }

  function formatPushedAt(iso: string | null | undefined): string {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toISOString().slice(0, 10);
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
          accept.
        </p>
        {hasImages ? (
          <Select
            value={selectedUri}
            onValueChange={setSelectedUri}
            disabled={queueing}
          >
            <SelectTrigger
              aria-label="Choose an openclaw-extended tag"
              className="font-mono text-xs"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {images.map((img) => {
                const date = formatPushedAt(img.pushed_at);
                const isDev = currentDevTag != null && img.tag === currentDevTag;
                const isProd = currentProdTag != null && img.tag === currentProdTag;
                const badge = isDev ? "current dev" : isProd ? "current prod" : null;
                return (
                  <SelectItem
                    key={img.image_uri}
                    value={img.image_uri}
                    className="font-mono text-xs"
                  >
                    <span className="flex items-center gap-2">
                      <span>
                        {img.tag}
                        {date ? `  ·  ${date}` : ""}
                      </span>
                      {badge ? (
                        <span
                          className={
                            isDev
                              ? "rounded-full border border-emerald-500/40 bg-emerald-500/10 px-1.5 py-0 text-[10px] font-sans uppercase tracking-wide text-emerald-300"
                              : "rounded-full border border-amber-500/40 bg-amber-500/10 px-1.5 py-0 text-[10px] font-sans uppercase tracking-wide text-amber-300"
                          }
                        >
                          {badge}
                        </span>
                      ) : null}
                    </span>
                  </SelectItem>
                );
              })}
              <SelectItem value={CUSTOM_IMAGE_VALUE} className="text-xs">
                Custom image URL…
              </SelectItem>
            </SelectContent>
          </Select>
        ) : (
          <p className="text-xs text-amber-300">
            Couldn&apos;t list ECR tags — enter the full image URL manually. See{" "}
            <code className="font-mono text-zinc-300">openclaw-version.json</code>{" "}
            on main for the current dev tag.
          </p>
        )}
        {(usingCustom || !hasImages) && (
          <Input
            type="text"
            placeholder="877352799272.dkr.ecr.us-east-1.amazonaws.com/isol8/openclaw-extended:2026.5.12-570e197"
            value={customImageUrl}
            onChange={(e) => setCustomImageUrl(e.target.value)}
            disabled={queueing}
            aria-label="New image URL"
            className="font-mono text-xs"
          />
        )}
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
          disabled={queueing || effectiveImageUrl.trim().length === 0}
        >
          {queueing ? "Queueing…" : "Queue image update"}
        </Button>
      </div>
    </div>
  );
}
