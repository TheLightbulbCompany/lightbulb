// apps/frontend/src/components/teams/cases/CaseIdentifierKey.tsx

// Ported from upstream Paperclip's CaseIdentifierKey (paperclip/ui/src/components/CaseIdentifierKey.tsx)
// (MIT, (c) 2025 Paperclip AI).

"use client";

import { useEffect, useRef, useState, type MouseEvent } from "react";
import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

/** The "Copied" flash shared by every click-to-copy value on the case pages. */
export function useCopied(): [boolean, (text: string) => void] {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  useEffect(() => () => clearTimeout(timerRef.current), []);

  const copy = (text: string) => {
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setCopied(false), 1500);
    });
  };
  return [copied, copy];
}

export function CopiedFlash() {
  return (
    <span
      role="status"
      aria-live="polite"
      className="pointer-events-none absolute bottom-full left-1/2 mb-1.5 inline-flex -translate-x-1/2 items-center gap-1 rounded-md bg-[var(--fg)] px-2 py-1 text-caption whitespace-nowrap text-[var(--plane-pane)]"
    >
      <Check className="h-3 w-3 shrink-0" />
      Copied
    </span>
  );
}

export function CaseCopyableToken({
  value,
  label,
  className,
  containerClassName,
  truncate = true,
  stopPropagation,
}: {
  value: string;
  label: string;
  className?: string;
  containerClassName?: string;
  truncate?: boolean;
  stopPropagation?: boolean;
}) {
  const [copied, copy] = useCopied();

  const handleCopy = (event: MouseEvent<HTMLButtonElement>) => {
    if (stopPropagation) {
      event.preventDefault();
      event.stopPropagation();
    }
    copy(value);
  };

  return (
    <span className={cn("relative inline-flex min-w-0 max-w-full", containerClassName)}>
      <button
        type="button"
        className={cn(
          "min-w-0 cursor-copy text-left transition-colors hover:text-[var(--fg)]",
          truncate ? "truncate" : "whitespace-normal break-all",
          className,
        )}
        title={value}
        aria-label={`Copy ${label} ${value}`}
        onClick={handleCopy}
      >
        {value}
      </button>
      {copied ? <CopiedFlash /> : null}
    </span>
  );
}

export function CaseIdentifierKey({
  identifier,
  caseKey,
  className,
  stopPropagation,
}: {
  identifier: string;
  caseKey?: string | null;
  className?: string;
  stopPropagation?: boolean;
}) {
  return (
    <span
      className={cn("inline-flex min-w-0 max-w-full items-center gap-2 whitespace-nowrap", className)}
      data-case-identity-group="true"
    >
      <CaseCopyableToken
        value={identifier}
        label="case ID"
        className="shrink-0 font-mono text-caption text-[var(--fg-mute)]"
        containerClassName="shrink-0"
        stopPropagation={stopPropagation}
      />
      {caseKey ? (
        <CaseCopyableToken
          value={caseKey}
          label="case key"
          className="font-mono text-caption text-[var(--fg-mute)]"
          stopPropagation={stopPropagation}
        />
      ) : null}
    </span>
  );
}
