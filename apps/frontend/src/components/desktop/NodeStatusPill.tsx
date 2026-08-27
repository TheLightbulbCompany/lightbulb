"use client";

import { useGateway } from "@/hooks/useGateway";

/**
 * Desktop-node ("local tools") status pill, rendered in both sidebars.
 *
 * Three states:
 * - connected → green "Local tools available"
 * - error     → amber "Local tools: connection failing" — previously an
 *   erroring node rendered NOTHING, indistinguishable from no desktop app
 *   at all (the Rust side only logged to /tmp/lightbulb-desktop.log)
 * - anything else (no desktop app, connecting, parked between retries) →
 *   nothing, as before
 */
export function NodeStatusPill() {
  const { nodeConnected, nodeStatus } = useGateway();
  const failing = !nodeConnected && nodeStatus === "error";
  if (!nodeConnected && !failing) return null;
  const color = nodeConnected ? "#16a34a" : "#d97706";
  return (
    <div
      style={{
        padding: "4px 12px",
        fontSize: "12px",
        color,
        display: "flex",
        alignItems: "center",
        gap: "6px",
      }}
    >
      <span
        style={{
          width: "6px",
          height: "6px",
          borderRadius: "50%",
          backgroundColor: color,
          display: "inline-block",
        }}
      />
      {nodeConnected ? "Local tools available" : "Local tools: connection failing"}
    </div>
  );
}
