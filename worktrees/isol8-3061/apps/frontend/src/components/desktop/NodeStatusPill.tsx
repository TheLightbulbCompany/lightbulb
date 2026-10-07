"use client";

import { useLiveEvents } from "@/hooks/useLiveEvents";

/**
 * Desktop-node ("local tools") status pill, rendered in both sidebars.
 *
 * Three states:
 * - connected → green "Mac connected" (not a grant for every tool)
 * - error     → warn "Mac connection failing" — previously an
 *   erroring node rendered NOTHING, indistinguishable from no desktop app
 *   at all (the Rust side only logged to /tmp/isol8-desktop.log)
 * - anything else (no desktop app, connecting, parked between retries) →
 *   nothing, as before
 */
export function NodeStatusPill() {
  const { nodeConnected, nodeStatus } = useLiveEvents();
  const failing = !nodeConnected && nodeStatus === "error";
  if (!nodeConnected && !failing) return null;
  const color = nodeConnected ? "var(--ok)" : "var(--warn)";
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
      {nodeConnected ? "Mac connected" : "Mac connection failing"}
    </div>
  );
}
