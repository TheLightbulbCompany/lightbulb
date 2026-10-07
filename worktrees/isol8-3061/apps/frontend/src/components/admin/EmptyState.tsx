import * as React from "react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface EmptyStateAction {
  label: string;
  /** If provided, the action renders as a Next.js `<Link>`. */
  href?: string;
  /** Otherwise, the action renders as a `<button>` with this click handler. */
  onClick?: () => void;
}

export interface EmptyStateProps {
  /** Optional decorative icon (e.g. a lucide icon component). */
  icon?: React.ReactNode;
  title: string;
  body: string;
  action?: EmptyStateAction;
  className?: string;
}

/**
 * Centered empty-state placeholder for "no results" or "day 1, no records yet"
 * surfaces. Implements the CEO U1 rule: never show a blank table — always
 * explain the state and (optionally) offer a next step.
 */
export function EmptyState({
  icon,
  title,
  body,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div
      role="status"
      className={cn(
        "flex w-full flex-col items-center justify-center gap-4 rounded-lg border border-dashed border-border bg-card px-6 py-16 text-center",
        className,
      )}
    >
      {icon ? (
        <div className="text-muted-foreground" aria-hidden="true">
          {icon}
        </div>
      ) : null}
      <h2 className="text-lg font-semibold text-foreground">{title}</h2>
      <p className="max-w-md text-sm text-muted-foreground">{body}</p>
      {action ? (
        action.href ? (
          <Button asChild variant="outline" size="sm">
            <Link href={action.href}>{action.label}</Link>
          </Button>
        ) : (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={action.onClick}
          >
            {action.label}
          </Button>
        )
      ) : null}
    </div>
  );
}
