"use client";

import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useSystemHealth, type HealthState } from "@/hooks/useSystemHealth";
import { Button } from "@/components/ui/button";

const DOT_STYLES: Record<HealthState, string> = {
  HEALTHY: "bg-ok",
  PARKED: "bg-[var(--midnight-faint)]",
  STARTING: "bg-warn animate-pulse",
  RECOVERING: "bg-warn animate-pulse",
  // GATEWAY_BUSY = parked backend WS, agent still working. Calm yellow with
  // a pulse so the user sees motion (the run is alive); NOT the red alarm
  // we used to show during a normal long Bedrock turn.
  GATEWAY_BUSY: "bg-warn animate-pulse",
  CONTAINER_DOWN: "bg-err",
};

const LABEL_STYLES: Record<HealthState, string> = {
  HEALTHY: "text-[var(--midnight-muted)]",
  PARKED: "text-[var(--midnight-muted)]",
  STARTING: "text-warn",
  RECOVERING: "text-warn",
  GATEWAY_BUSY: "text-warn",
  CONTAINER_DOWN: "text-err",
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

  // Healthy is the normal case: the sidebar shows health only when something is wrong.
  if (state === "HEALTHY") return null;

  return (
    <div
      className="flex items-center gap-2 px-3 py-2 rounded-md text-xs"
      title={reason}
    >
      {/* Status dot */}
      <span
        className={cn("h-2 w-2 rounded-full shrink-0", DOT_STYLES[state])}
      />

      {/* Label */}
      <span className={cn("truncate flex-1", LABEL_STYLES[state])}>
        {state === "RECOVERING"
            ? reason
            : state === "STARTING"
              ? "Starting..."
              : state === "GATEWAY_BUSY"
                ? "Agent thinking..."
                : reason}
      </span>

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
