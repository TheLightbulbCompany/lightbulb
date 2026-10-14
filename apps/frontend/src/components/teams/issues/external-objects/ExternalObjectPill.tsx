// Ported from upstream Paperclip's ExternalObjectPill.tsx
// (paperclip/ui/src/components/ExternalObjectPill.tsx) (MIT, (c) 2025 Paperclip AI).

import type { ReactNode } from "react";
import type {
  ExternalObjectLivenessState,
  ExternalObjectStatusCategory,
} from "@/components/teams/shared/types";
import { cn } from "@/lib/utils";
import { ExternalObjectStatusIcon } from "./ExternalObjectStatusIcon";
import {
  externalObjectColor,
  externalObjectDisplayLabel,
  externalObjectDisplayStatusLabel,
  externalObjectIconsByKey,
  externalObjectLivenessLabel,
  externalObjectLivenessOverlay,
  externalObjectProviderLabel,
  githubObjectLabel,
} from "./external-objects";

export interface ExternalObjectPillData {
  providerKey: string | null;
  objectType: string | null;
  displayKey?: string | null;
  iconKey?: string | null;
  statusCategory: ExternalObjectStatusCategory;
  liveness: ExternalObjectLivenessState;
  displayTitle?: string | null;
  statusLabel?: string | null;
  statusIconKey?: string | null;
  url?: string | null;
}

/** A merged pull request: the provider says so by icon key or by label. */
export function isMergedExternalObject(object: ExternalObjectPillData, statusLabel: string): boolean {
  return object.statusIconKey === "git-merge" || statusLabel.toLowerCase() === "merged";
}

interface ExternalObjectPillProps {
  object: ExternalObjectPillData;
  /** Optional external mention count (renders as `×N` when > 1). */
  sourceCount?: number;
  /** Optional source-mention summary used in the pill's `title` attribute. */
  sourceSummary?: string | null;
  className?: string;
  /** Optional rendered label override. Defaults to the status icon + value. */
  children?: ReactNode;
  /**
   * If true the pill renders without a hover treatment (used inside
   * non-interactive contexts). Defaults to false.
   */
  inert?: boolean;
  /**
   * Hide the provider icon when the surrounding UI already names the provider.
   * The status icon still renders as the single state glyph inside the pill.
   */
  showProviderIcon?: boolean;
}

export function ExternalObjectPill({
  object,
  sourceCount,
  sourceSummary,
  className,
  children,
  inert,
  showProviderIcon = true,
}: ExternalObjectPillProps) {
  const overlay = externalObjectLivenessOverlay[object.liveness] ?? "";
  const providerLabel = externalObjectProviderLabel(object.providerKey);
  const displayKey = externalObjectDisplayLabel(object.providerKey, object.objectType, object.displayKey);
  const statusLabel = externalObjectDisplayStatusLabel(object);
  const merged = isMergedExternalObject(object, statusLabel);
  const githubLabel = object.providerKey === "github" ? githubObjectLabel(object.url) : null;
  const base = githubLabel ?? object.displayTitle?.trim() ?? displayKey;
  const valueLabel = statusLabel ? `${base} - ${statusLabel}` : base;
  const livenessLabel = externalObjectLivenessLabel(object.liveness);
  const ProviderIcon = externalObjectIconsByKey[object.iconKey ?? ""];
  const ariaLabel = `${displayKey} — ${statusLabel}${
    object.liveness === "fresh" || object.liveness === "unknown" ? "" : ` (${livenessLabel})`
  }${object.displayTitle ? `: ${object.displayTitle}` : ""}`;

  const interactive = !inert && Boolean(object.url);
  const classNames = cn(
    "inline-flex max-w-full items-center gap-1 rounded-full border border-[var(--edge)] px-2 py-0.5 text-caption no-underline",
    overlay,
    interactive
      && "hover:bg-[var(--cream-deep)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--link)]",
    className,
  );
  // The colour is applied to the text only — the border style comes from the
  // overlay (dashed for stale / auth / unreachable).
  const style = { color: externalObjectColor(merged ? "merged" : object.statusCategory) };
  const titleAttr = sourceSummary
    ? `${object.displayTitle ?? displayKey} — ${sourceSummary}`
    : object.displayTitle ?? displayKey;
  const labelText = children ?? (
    <>
      <ExternalObjectStatusIcon
        category={object.statusCategory}
        liveness={object.liveness}
        statusIconKey={merged ? object.statusIconKey ?? "git-merge" : object.statusIconKey}
        sizeClassName="h-3 w-3"
        label={`${providerLabel}: ${statusLabel}`}
      />
      <span className="max-w-[16rem] truncate font-medium">{valueLabel}</span>
    </>
  );
  const innerContent = (
    <>
      {showProviderIcon && ProviderIcon ? (
        <ProviderIcon aria-hidden="true" className="h-3 w-3 shrink-0" />
      ) : null}
      <span className="inline-flex min-w-0 items-center gap-1">
        {labelText}
      </span>
      {typeof sourceCount === "number" && sourceCount > 1 ? (
        <span className="tabular-nums text-2xs font-medium opacity-80">×{sourceCount}</span>
      ) : null}
    </>
  );

  if (interactive && object.url) {
    return (
      <a
        href={object.url}
        target="_blank"
        rel="noopener noreferrer"
        data-mention-kind="external-object"
        data-external-status={object.statusCategory}
        data-external-liveness={object.liveness}
        className={classNames}
        style={style}
        title={titleAttr}
        aria-label={ariaLabel}
      >
        {innerContent}
      </a>
    );
  }

  return (
    <span
      data-mention-kind="external-object"
      data-external-status={object.statusCategory}
      data-external-liveness={object.liveness}
      className={classNames}
      style={style}
      title={titleAttr}
      aria-label={ariaLabel}
    >
      {innerContent}
    </span>
  );
}
