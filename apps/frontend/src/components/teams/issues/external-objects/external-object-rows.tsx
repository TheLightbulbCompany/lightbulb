// Ported from upstream Paperclip's external-object-rows.tsx
// (paperclip/ui/src/components/issue-properties/external-object-rows.tsx)
// (MIT, (c) 2025 Paperclip AI).

"use client";

import { useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { ExternalObjectStatusIcon } from "./ExternalObjectStatusIcon";
import { isMergedExternalObject } from "./ExternalObjectPill";
import {
  externalObjectColor,
  externalObjectDisplayLabel,
  externalObjectDisplayStatusLabel,
  externalObjectIconsByKey,
  externalObjectProviderLabel,
  externalObjectToneSeverity,
  externalObjectTypeLabel,
  githubObjectLabel,
} from "./external-objects";
import { useIssueExternalObjects, type IssueExternalObjectGroup } from "./useIssueExternalObjects";

const EXTERNAL_OBJECT_PROPERTY_PREVIEW_COUNT = 5;

// Same row markup as IssueProperties' PropertyRow, with a node label (these
// rows lead with the provider icon).
function PropertyRow({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div
      className="grid grid-cols-[100px_1fr] gap-3 items-baseline py-2 text-ui"
      style={{ color: "var(--midnight-soft)", borderBottom: "1px solid var(--edge)" }}
    >
      <dt className="section-label" style={{ marginTop: 0, marginBottom: 0 }}>
        <b>{label}</b>
      </dt>
      <dd className="min-w-0 truncate">{children}</dd>
    </div>
  );
}

function externalObjectRowDisplayKey(group: IssueExternalObjectGroup): string {
  const { pill } = group;
  const displayKey = pill.displayKey?.trim();
  if (displayKey) return displayKey;
  if (pill.providerKey === "github") {
    if (pill.objectType === "pull_request") return "Github PR";
    if (pill.objectType === "issue") return "Github Issue";
  }
  return externalObjectDisplayLabel(pill.providerKey, pill.objectType);
}

function externalObjectRowLabel(group: IssueExternalObjectGroup): ReactNode {
  const displayKey = externalObjectRowDisplayKey(group);
  const Icon = externalObjectIconsByKey[group.pill.iconKey ?? ""];
  return (
    // Wraps inside the 100px label column like the rail's other labels
    // ("Related tasks"): a nowrap/truncate span made the label's min-content its
    // full width, so "GitHub pull request" overflowed under the value.
    <span className="inline-flex min-w-0 items-start gap-1" title={displayKey}>
      {Icon ? <Icon aria-hidden="true" className="h-3.5 w-3.5 shrink-0 mt-0.5" /> : null}
      <span className="min-w-0 break-words">{displayKey}</span>
    </span>
  );
}

function ExternalObjectPropertyValue({ group }: { group: IssueExternalObjectGroup }) {
  const { pill, sourceLabels } = group;
  const statusLabel = externalObjectDisplayStatusLabel(pill);
  const providerLabel = externalObjectProviderLabel(pill.providerKey);
  const typeLabel = externalObjectTypeLabel(pill.objectType);
  const githubLabel = pill.providerKey === "github" ? githubObjectLabel(pill.url) : null;
  const base = githubLabel ?? pill.displayTitle?.trim() ?? externalObjectRowDisplayKey(group);
  const value = statusLabel ? `${base} - ${statusLabel}` : base;
  const merged = isMergedExternalObject(pill, statusLabel);
  const titleBase = pill.displayTitle ?? value;
  const title = sourceLabels.length > 0 ? `${titleBase} - ${sourceLabels.join(", ")}` : titleBase;
  const ariaLabel = `${providerLabel} ${typeLabel} - ${statusLabel}: ${pill.displayTitle ?? value}`;
  const content = (
    <>
      <ExternalObjectStatusIcon
        category={pill.statusCategory}
        liveness={pill.liveness}
        statusIconKey={merged ? pill.statusIconKey ?? "git-merge" : pill.statusIconKey}
        sizeClassName="h-3.5 w-3.5"
        label={`${providerLabel}: ${statusLabel}`}
      />
      <span className="min-w-0 truncate">{value}</span>
    </>
  );
  const className = cn(
    "inline-flex min-w-0 max-w-full items-center gap-1.5 text-ui no-underline",
    pill.url ? "hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--link)]" : "",
  );
  const style = { color: externalObjectColor(merged ? "merged" : pill.statusCategory) };

  if (pill.url) {
    return (
      <a
        href={pill.url}
        target="_blank"
        rel="noopener noreferrer"
        data-mention-kind="external-object"
        data-external-status={pill.statusCategory}
        data-external-liveness={pill.liveness}
        className={className}
        style={style}
        title={title}
        aria-label={ariaLabel}
      >
        {content}
      </a>
    );
  }

  return (
    <span
      data-mention-kind="external-object"
      data-external-status={pill.statusCategory}
      data-external-liveness={pill.liveness}
      className={className}
      style={style}
      title={title}
      aria-label={ariaLabel}
    >
      {content}
    </span>
  );
}

export function ExternalObjectRows({
  externalObjects,
  externalObjectsError,
  onRetryExternalObjects,
}: {
  externalObjects?: IssueExternalObjectGroup[];
  externalObjectsError?: boolean;
  onRetryExternalObjects?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);

  if (externalObjectsError) {
    return (
      <PropertyRow label="External objects">
        <span className="text-caption text-[var(--fg-mute)]">
          Couldn&apos;t load external objects.
          {onRetryExternalObjects ? (
            <>
              {" "}
              <button
                type="button"
                className="text-[var(--link)] underline-offset-2 hover:underline"
                onClick={onRetryExternalObjects}
              >
                Retry
              </button>
            </>
          ) : null}
        </span>
      </PropertyRow>
    );
  }

  if (!externalObjects || externalObjects.length === 0) return null;

  const sortedExternalObjects = [...externalObjects].sort(
    (a, b) =>
      externalObjectToneSeverity(b.group.object?.statusTone) - externalObjectToneSeverity(a.group.object?.statusTone),
  );
  const visibleExternalObjects = expanded
    ? sortedExternalObjects
    : sortedExternalObjects.slice(0, EXTERNAL_OBJECT_PROPERTY_PREVIEW_COUNT);
  const hiddenExternalObjectCount = sortedExternalObjects.length - visibleExternalObjects.length;

  return (
    <>
      {visibleExternalObjects.map((externalObject) => {
        const { pill, group } = externalObject;
        return (
          <PropertyRow
            key={group.object?.id ?? `${pill.providerKey}:${pill.objectType}:${pill.url ?? "anon"}`}
            label={externalObjectRowLabel(externalObject)}
          >
            <ExternalObjectPropertyValue group={externalObject} />
          </PropertyRow>
        );
      })}
      {expanded || hiddenExternalObjectCount > 0 ? (
        <PropertyRow label="References">
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-full border border-[var(--edge)] px-2 py-0.5 text-caption text-[var(--fg-mute)] transition-colors hover:bg-[var(--cream-deep)] hover:text-[var(--fg)]"
            onClick={() => setExpanded((next) => !next)}
            aria-label={expanded ? "Show fewer items" : `Show ${hiddenExternalObjectCount} more items`}
          >
            {expanded ? "Show less" : `Show ${hiddenExternalObjectCount} more`}
          </button>
        </PropertyRow>
      ) : null}
    </>
  );
}

/**
 * The rail's external-object rows for one issue. Reads its own data, like the
 * rail's Sub-tasks row. Keyed by issue so "Show more" resets when the rail
 * moves to another issue (upstream resets it in an effect on the list).
 */
export function IssueExternalObjectRows({ issueId }: { issueId: string }) {
  const { groups, isError, refetch } = useIssueExternalObjects(issueId);
  return (
    <ExternalObjectRows
      key={issueId}
      externalObjects={groups}
      externalObjectsError={isError}
      onRetryExternalObjects={refetch}
    />
  );
}
