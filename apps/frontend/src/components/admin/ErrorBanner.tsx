import * as React from "react";
import { AlertTriangle, Info, XCircle } from "lucide-react";

import { cn } from "@/lib/utils";

export type ErrorBannerVariant = "warning" | "error" | "info";

export interface ErrorBannerProps {
  /** Human-readable error message. */
  error: string;
  /** Optional source label, e.g. `"Stripe"`, `"Clerk"`, `"PostHog"`. */
  source?: string;
  /** Color treatment. Defaults to `"error"`. */
  variant?: ErrorBannerVariant;
  className?: string;
}

const VARIANT_CLASSES: Record<ErrorBannerVariant, string> = {
  error: "border-l-2 border-err bg-card text-err",
  warning: "border-l-2 border-warn bg-card text-warn",
  info: "border-l-2 border-info bg-card text-info",
};

const VARIANT_ICONS: Record<
  ErrorBannerVariant,
  React.ComponentType<{ className?: string }>
> = {
  error: XCircle,
  warning: AlertTriangle,
  info: Info,
};

const VARIANT_ROLE: Record<ErrorBannerVariant, "alert" | "status"> = {
  error: "alert",
  warning: "alert",
  info: "status",
};

/**
 * Inline banner for surfacing partial-failure messages on admin pages — e.g.
 * "Stripe lookup timed out, showing cached data" while Clerk and PostHog
 * sections still render normally.
 */
export function ErrorBanner({
  error,
  source,
  variant = "error",
  className,
}: ErrorBannerProps) {
  const Icon = VARIANT_ICONS[variant];
  return (
    <div
      role={VARIANT_ROLE[variant]}
      className={cn(
        "flex items-start gap-3 rounded-md border px-3 py-2 text-sm",
        VARIANT_CLASSES[variant],
        className,
      )}
      data-variant={variant}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="flex-1">
        {source ? (
          <span className="mr-2 font-semibold">{source}:</span>
        ) : null}
        <span>{error}</span>
      </div>
    </div>
  );
}
