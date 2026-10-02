// apps/frontend/src/components/teams/cases/CaseFieldsPanel.tsx

// Ported from upstream Paperclip's CaseFieldsPanel (paperclip/ui/src/components/CaseFieldsPanel.tsx)
// (MIT, (c) 2025 Paperclip AI).
//
// The generic key-value renderer for a case's `fields` JSON blob. The server
// stores arbitrary agent-authored JSON, so the UI renders by *value type*
// (never crash on unexpected shapes) and preserves the skill's key insertion
// order (does NOT alphabetize). Only `CaseFieldValue` is ported: upstream's
// `CaseFieldsPanel` card has no caller at the ported tag.

"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { Check, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { handleShellLinkClick } from "@/lib/shellNav";
import { cn } from "@/lib/utils";
import { CopiedFlash, useCopied } from "./CaseIdentifierKey";
import { caseHref, issueHref } from "./caseNav";

const URL_RE = /^https?:\/\/\S+$/i;
const CASE_ID_RE = /^[A-Z][A-Z0-9]*-C\d+$/;
const ISSUE_ID_RE = /^[A-Z][A-Z0-9]*-\d+$/;
const ISSUE_ID_IN_TEXT_RE = /\b[A-Z][A-Z0-9]*-\d+\b/g;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A muted em-dash for null / empty / missing values. */
function EmptyValue() {
  return <span className="text-[var(--fg-mute)]">—</span>;
}

/** Stands in for upstream's IssueReferencePill: a task chip that opens the task. */
export function IssueReferencePill({
  issue,
}: {
  issue: { identifier: string; title: string; status?: string };
}) {
  const href = issueHref(issue.identifier);
  return (
    <Link
      href={href}
      prefetch={false}
      onClick={(e) => handleShellLinkClick(e, href)}
      title={issue.title}
      className="inline-flex max-w-full items-center gap-1 rounded-full border border-[var(--edge)] px-2 py-0.5 text-xs text-[var(--fg-soft)] transition-colors hover:bg-[var(--plane-raised)]"
    >
      {issue.status ? <StatusIcon status={issue.status} className="h-3 w-3" /> : null}
      <span className="shrink-0 font-mono">{issue.identifier}</span>
    </Link>
  );
}

function isIssueIdentifierField(fieldKey: string | undefined): boolean {
  if (!fieldKey) return false;
  const normalized = fieldKey.toLowerCase().replace(/[^a-z0-9]/g, "");
  return normalized.includes("issueidentifier") || normalized.includes("taskidentifier");
}

function stringifyCopyValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value, null, 2);
}

function extractIssueIdentifiers(value: unknown, fieldKey?: string): string[] {
  if (!isIssueIdentifierField(fieldKey)) {
    return typeof value === "string" && ISSUE_ID_RE.test(value.trim()) ? [value.trim()] : [];
  }

  const identifiers: string[] = [];
  const add = (candidate: unknown) => {
    if (typeof candidate !== "string") return;
    for (const match of candidate.matchAll(ISSUE_ID_IN_TEXT_RE)) identifiers.push(match[0]);
  };

  if (Array.isArray(value)) value.forEach(add);
  else add(value);

  return [...new Set(identifiers)];
}

function CopyableCompactValue({
  value,
  children,
  className,
}: {
  value: unknown;
  children: ReactNode;
  className?: string;
}) {
  const text = stringifyCopyValue(value);
  const [copied, copy] = useCopied();

  return (
    <span className="relative inline-flex min-w-0 max-w-full">
      <button
        type="button"
        className={cn("min-w-0 max-w-full cursor-copy truncate text-left transition-colors hover:text-[var(--fg)]", className)}
        title={text}
        onClick={() => copy(text)}
      >
        {children}
      </button>
      {copied ? <CopiedFlash /> : null}
    </span>
  );
}

function StringValue({ value, variant }: { value: string; variant: "compact" | "full" }) {
  const trimmed = value.trim();
  if (trimmed === "") return <EmptyValue />;
  if (URL_RE.test(trimmed)) {
    return (
      <a
        href={trimmed}
        target="_blank"
        rel="noreferrer"
        className={cn(
          "inline-flex max-w-full items-center gap-0.5 text-sm text-[var(--link)] hover:underline",
          variant === "compact" ? "truncate" : "break-all",
        )}
        title={trimmed}
      >
        <span className={variant === "compact" ? "truncate" : "break-all"}>{trimmed}</span>
        <span aria-hidden>↗</span>
      </a>
    );
  }
  if (CASE_ID_RE.test(trimmed)) {
    return (
      <Link
        href={caseHref(trimmed)}
        prefetch={false}
        onClick={(e) => handleShellLinkClick(e, caseHref(trimmed))}
        className="font-mono text-sm text-[var(--link)] hover:underline"
      >
        {trimmed}
      </Link>
    );
  }
  if (variant === "compact") {
    return (
      <CopyableCompactValue value={value} className="text-sm">
        {value}
      </CopyableCompactValue>
    );
  }
  return <span className="text-sm break-words">{value}</span>;
}

export function CaseFieldValue({
  value,
  fieldKey,
  variant = "full",
}: {
  value: unknown;
  fieldKey?: string;
  variant?: "compact" | "full";
}) {
  if (value === null || value === undefined) return <EmptyValue />;

  const issueIdentifiers = extractIssueIdentifiers(value, fieldKey);
  if (issueIdentifiers.length > 0) {
    return (
      <span className="flex min-w-0 flex-wrap items-center gap-1.5">
        {issueIdentifiers.map((identifier) => (
          <IssueReferencePill key={identifier} issue={{ identifier, title: identifier }} />
        ))}
      </span>
    );
  }

  if (typeof value === "string") return <StringValue value={value} variant={variant} />;

  if (typeof value === "number") {
    if (!Number.isFinite(value)) return <span className="text-sm">{String(value)}</span>;
    if (variant === "compact") {
      return (
        <CopyableCompactValue value={value} className="text-sm tabular-nums">
          {value.toLocaleString()}
        </CopyableCompactValue>
      );
    }
    return <span className="text-sm tabular-nums">{value.toLocaleString()}</span>;
  }

  if (typeof value === "boolean") {
    // Upstream renders `false` as the same dash as a missing value; an
    // explicit false is an answer, so it gets its own mark.
    return value ? (
      <Check className="h-4 w-4 text-[var(--live)]" aria-label="true" />
    ) : (
      <X className="h-4 w-4 text-[var(--fg-mute)]" aria-label="false" />
    );
  }

  if (Array.isArray(value)) {
    if (value.length === 0) return <EmptyValue />;
    if (variant === "compact") {
      return (
        <CopyableCompactValue value={value} className="text-sm text-[var(--fg-mute)]">
          {stringifyCopyValue(value)}
        </CopyableCompactValue>
      );
    }
    return (
      <div className="flex flex-wrap justify-start gap-1">
        {value.map((item, index) => (
          <Badge key={index} variant="secondary" className="font-normal">
            {typeof item === "string" || typeof item === "number" || typeof item === "boolean"
              ? String(item)
              : JSON.stringify(item)}
          </Badge>
        ))}
      </div>
    );
  }

  if (isPlainObject(value)) {
    if (variant === "compact") {
      return (
        <CopyableCompactValue value={value} className="font-mono text-xs text-[var(--fg-mute)]">
          {JSON.stringify(value)}
        </CopyableCompactValue>
      );
    }
    return (
      <pre className="max-w-full whitespace-pre-wrap break-words font-mono text-xs text-[var(--fg-mute)]">
        {JSON.stringify(value, null, 2)}
      </pre>
    );
  }

  return <span className="text-sm">{String(value)}</span>;
}
