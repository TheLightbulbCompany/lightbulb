"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { Card } from "@/components/admin/Card";
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
  forceApplyImageUpdate,
  queueImageUpdate,
  reapplyOwnerConfig,
  reprovisionContainer,
  resizeContainer,
  startContainer,
  stopContainer,
} from "@/app/admin/_actions/container";
import { AWS_REGION } from "@/lib/aws";

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
 * Note on Resize: post-flat-fee (2026-04 pivot) every user runs a
 * plan-sized box (standard 1 vCPU / 4 GB; Ultra Pro 2 vCPU / 8 GB), so resize
 * is now a "re-apply standard sizing" recovery action — it forces the ECS
 * service to roll its task definition with the canonical per-user resource
 * profile. The backend's resize endpoint accepts and ignores the legacy `tier` argument.
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
  // Default to the PINNED prod image, not the newest build.
  //
  // This used to default to images[0] (freshest). But "Apply now" force-rolls
  // onto whatever is selected, and the most common reason to touch this
  // control is a restart to pick up regenerated config on the image the owner
  // is ALREADY on. Defaulting to the newest build turned that routine action
  // into a silent version change — nearly rolled a prod owner off the pinned
  // tag onto an unpromoted build. Rolling forward stays one click; it is just
  // deliberate now.
  //
  // Falls back to the newest row if the pin is somehow absent from the list,
  // then to "custom" when ECR errored out, so the free-text field still gets
  // a useful starting state.
  const defaultUri = React.useMemo(() => {
    if (!hasImages) return CUSTOM_IMAGE_VALUE;
    const pinned = currentProdTag
      ? images.find((img) => img.tag === currentProdTag)
      : undefined;
    return (pinned ?? images[0]).image_uri;
  }, [hasImages, images, currentProdTag]);

  const [selectedUri, setSelectedUri] = React.useState<string>(defaultUri);
  const [customImageUrl, setCustomImageUrl] = React.useState("");
  const [imageDescription, setImageDescription] = React.useState("");
  const [queueing, setQueueing] = React.useState(false);

  // Whether the pending force-roll would CHANGE the owner's image rather than
  // just restart them on the one they are already pinned to. Surfaced in the
  // confirm dialog's title so a version change can't happen by accident on a
  // control whose routine use is "restart to pick up new config".
  const rollsOffPin =
    hasImages &&
    currentProdTag != null &&
    selectedUri !== CUSTOM_IMAGE_VALUE &&
    images.find((img) => img.image_uri === selectedUri)?.tag !== currentProdTag;

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
        if (hasImages) setSelectedUri(defaultUri);
      }
    } finally {
      setQueueing(false);
    }
  }

  // Force-apply NOW: queues the update then rolls the container onto the new
  // image immediately (same apply_update() path the owner banner uses — reuses
  // the on-disk config, strips removed-schema keys first, preserves EFS state).
  // The container briefly restarts, so this is gated behind a typed confirm.
  async function handleForceApplyImageUpdate() {
    clearStatus();
    const trimmedUrl = effectiveImageUrl.trim();
    if (!trimmedUrl) {
      setError("Image URL is required.");
      return;
    }
    setQueueing(true);
    try {
      const result = await forceApplyImageUpdate(
        userId,
        trimmedUrl,
        imageDescription.trim() || undefined,
      );
      applyResult("Force-apply image update", result.ok, result.error);
      if (result.ok) {
        setCustomImageUrl("");
        setImageDescription("");
        if (hasImages) setSelectedUri(defaultUri);
      }
    } finally {
      setQueueing(false);
    }
  }

  async function handleReapplyConfig() {
    clearStatus();
    const result = await reapplyOwnerConfig(userId);
    if (result.ok) {
      const restored =
        result.data && typeof result.data === "object" && "restored_subtrees" in result.data
          ? (result.data as { restored_subtrees?: string[] }).restored_subtrees
          : undefined;
      setNotice(
        restored && restored.length > 0
          ? `Regenerate config succeeded (restored runtime subtrees: ${restored.join(", ")}).`
          : "Regenerate config succeeded.",
      );
      router.refresh();
    } else {
      setError(`Regenerate config failed: ${result.error ?? "unknown_error"}`);
    }
  }

  function formatPushedAt(iso: string | null | undefined): string {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toISOString().slice(0, 10);
  }

  return (
    <Card title="Container actions">
      <div className="space-y-3">
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
        <div className="space-y-2 rounded-lg border border-border p-3">
          <h3 className="text-xs font-medium uppercase tracking-caps text-muted-foreground">
            Re-apply standard sizing
          </h3>
          <p className="text-xs text-muted-foreground">
            Forces the ECS service to roll its task definition back to the
            owner&apos;s plan-sized profile (standard 1 vCPU / 4 GB; Ultra
            Pro 2 vCPU / 8 GB). Use as a recovery action when a container is
            wedged on a stale task def.
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

        {/* Config regen from the current generator (dashboard twin of
            scripts/reapply_openclaw_config.py). Config-only, no restart —
            the counterpart to "Apply now" below, which rolls the image but
            reuses the on-disk config verbatim. */}
        <div className="space-y-2 rounded-lg border border-border p-3">
          <h3 className="text-xs font-medium uppercase tracking-caps text-muted-foreground">
            Regenerate config
          </h3>
          <p className="text-xs text-muted-foreground">
            Re-renders <span className="font-mono">openclaw.json</span> from the
            current config generator via the provisioning write path — clears
            stale keys a deep-merge patch would preserve — then restores the
            runtime-appended subtrees the regen drops (channel pairings, route
            bindings, identity). Config-only: no restart, EFS workspace/agent
            state untouched. Pair with{" "}
            <strong className="text-foreground">Apply now</strong> below when the
            change needs a gateway restart to activate (e.g. plugin
            enablement).
          </p>
          <ConfirmActionDialog
            confirmText={userId}
            actionLabel="Regenerate config"
            onConfirm={handleReapplyConfig}
          >
            <Button type="button" variant="default" size="sm">
              Regenerate config
            </Button>
          </ConfirmActionDialog>
        </div>

        {/* Track 2 image update (banner + user-driven apply). Non-destructive
            alternative to Reprovision — container keeps running on the old image
            until the owner clicks "Update now" in their chat banner. */}
        <div className="space-y-2 rounded-lg border border-border p-3">
          <h3 className="text-xs font-medium uppercase tracking-caps text-muted-foreground">
            Queue image update
          </h3>
          <p className="text-xs text-muted-foreground">
            <strong className="text-foreground">Queue</strong> creates a pending Track 2
            update — the owner sees a banner in chat and chooses when to apply
            (Update now / Tonight / Remind later); the container keeps running on the
            current image until they accept.{" "}
            <strong className="text-foreground">Apply now</strong> force-rolls the
            container onto the selected image immediately (no banner wait). Both reuse
            the container&apos;s existing on-disk config and preserve EFS
            workspace/agent state — Apply now briefly restarts the container.
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
                  const badges = [
                    ...(isDev ? [{ label: "current dev", tone: "dev" as const }] : []),
                    ...(isProd ? [{ label: "current prod", tone: "prod" as const }] : []),
                  ];
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
                        {badges.map((b) => (
                          <span
                            key={b.label}
                            className={
                              b.tone === "dev"
                                ? "rounded-sm border border-ok px-1.5 py-0 text-2xs font-sans font-semibold uppercase tracking-caps text-ok"
                                : "rounded-sm border border-warn px-1.5 py-0 text-2xs font-sans font-semibold uppercase tracking-caps text-warn"
                            }
                          >
                            {b.label}
                          </span>
                        ))}
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
            <p className="text-xs text-warn">
              Couldn&apos;t list ECR tags — enter the full image URL manually. See{" "}
              <code className="font-mono text-muted-foreground">openclaw-version.json</code>{" "}
              on main for the current dev tag.
            </p>
          )}
          {(usingCustom || !hasImages) && (
            <Input
              type="text"
              placeholder={`877352799272.dkr.ecr.${AWS_REGION}.amazonaws.com/lightbulb/openclaw-extended:<tag>`}
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
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleQueueImageUpdate}
              disabled={queueing || effectiveImageUrl.trim().length === 0}
            >
              {queueing ? "Working…" : "Queue image update"}
            </Button>
            <ConfirmActionDialog
              confirmText={userId}
              actionLabel={
                usingCustom
                  ? "Force-apply a CUSTOM image now"
                  : rollsOffPin
                    ? "Force-apply a NON-PINNED image now (changes their version)"
                    : "Force-apply image update now (same pinned image)"
              }
              destructive
              onConfirm={handleForceApplyImageUpdate}
            >
              <Button
                type="button"
                variant="default"
                size="sm"
                disabled={queueing || effectiveImageUrl.trim().length === 0}
              >
                {queueing ? "Working…" : "Apply now (force)"}
              </Button>
            </ConfirmActionDialog>
          </div>
        </div>
      </div>
    </Card>
  );
}
