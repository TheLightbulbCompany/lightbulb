"use client";

import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useSystemHealth, type HealthState } from "@/hooks/useSystemHealth";
import { Button } from "@/components/ui/button";

// Only the broken states take a line in the sidebar. Healthy, parked (asleep,
// wakes on use), starting and mid-turn (GATEWAY_BUSY) are all normal and render nothing.
const BROKEN: Partial<Record<HealthState, { dot: string; label: string }>> = {
  RECOVERING: { dot: "bg-warn animate-pulse", label: "text-warn" },
  CONTAINER_DOWN: { dot: "bg-err", label: "text-err" },
};

export function HealthIndicator({
  onRecoveryReprovision,
}: {
  /** Called when recovery triggers a reprovision, so parent can show ProvisioningStepper */
  onRecoveryReprovision?: () => void;
}) {
  const {
    state,
    reason,
    canRecover,
    actionLabel,
    recover,
    isRecovering,
  } = useSystemHealth();

  const handleRecover = async () => {
    const result = await recover();
    if (result?.action === "reprovision" && onRecoveryReprovision) {
      onRecoveryReprovision();
    }
  };

  const broken = BROKEN[state];
  if (!broken) return null;

  return (
    <div
      className="flex items-center gap-2 px-3 py-2 rounded-md text-xs"
      title={reason}
    >
      {/* Status dot */}
      <span
        className={cn("h-2 w-2 rounded-full shrink-0", broken.dot)}
      />

      {/* Label */}
      <span className={cn("truncate flex-1", broken.label)}>{reason}</span>

      {/* Action button */}
      {isRecovering ? (
        <Loader2 className="h-3 w-3 animate-spin text-[var(--midnight-muted)]" />
      ) : canRecover && actionLabel ? (
        <Button
          variant="ghost"
          size="sm"
          onClick={handleRecover}
          className="h-5 px-2 text-2xs font-medium hover:bg-[var(--cream-deep)]"
        >
          {actionLabel}
        </Button>
      ) : null}
    </div>
  );
}
