"use client";

import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useSystemHealth, type HealthState } from "@/hooks/useSystemHealth";
import { Button } from "@/components/ui/button";

const DOT_STYLES: Record<HealthState, string> = {
  HEALTHY: "bg-[#2d8a4e]",
  STARTING: "bg-yellow-500 animate-pulse",
  RECOVERING: "bg-yellow-500 animate-pulse",
  // GATEWAY_BUSY = parked backend WS, agent still working. Calm yellow with
  // a pulse so the user sees motion (the run is alive); NOT the red alarm
  // we used to show during a normal long Bedrock turn.
  GATEWAY_BUSY: "bg-yellow-500 animate-pulse",
  GATEWAY_DOWN_DEAD: "bg-red-500",
  CONTAINER_DOWN: "bg-red-500",
};

const LABEL_STYLES: Record<HealthState, string> = {
  HEALTHY: "text-[#8a8578]",
  STARTING: "text-yellow-600",
  RECOVERING: "text-yellow-600",
  GATEWAY_BUSY: "text-yellow-600",
  GATEWAY_DOWN_DEAD: "text-red-500",
  CONTAINER_DOWN: "text-red-500",
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

  return (
    <div
      className={cn(
        "flex items-center gap-2 px-3 py-2 rounded-md text-xs",
        state === "HEALTHY" ? "opacity-80" : "opacity-100",
      )}
      title={reason}
    >
      {/* Status dot */}
      <span
        className={cn("h-2 w-2 rounded-full shrink-0", DOT_STYLES[state])}
      />

      {/* Label */}
      <span className={cn("truncate flex-1", LABEL_STYLES[state])}>
        {state === "HEALTHY"
          ? "Connected"
          : state === "RECOVERING"
            ? reason
            : state === "STARTING"
              ? "Starting..."
              : state === "GATEWAY_BUSY"
                ? "Agent thinking..."
                : reason}
      </span>

      {/* Action button */}
      {isRecovering ? (
        <Loader2 className="h-3 w-3 animate-spin text-[#8a8578]" />
      ) : canRecover && actionLabel ? (
        <Button
          variant="ghost"
          size="sm"
          onClick={handleRecover}
          className="h-5 px-2 text-[10px] font-medium hover:bg-[#e8e4db]"
        >
          {actionLabel}
        </Button>
      ) : null}
    </div>
  );
}
