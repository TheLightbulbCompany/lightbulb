"use client";

import { useEngineDirectory } from "@/components/messaging/engineDirectory";
import { bridge } from "@/components/messaging/localTerminal";

/** The engine's background service is off: say so, with the one repair. A failed Fix leaves this showing. */
export function EngineFault() {
  const { down } = useEngineDirectory();
  if (!down) return null;
  return (
    <div style={{ padding: "4px 12px", fontSize: "12px", color: "var(--warn)", display: "flex", alignItems: "center", gap: "6px" }}>
      <span style={{ width: "6px", height: "6px", borderRadius: "50%", backgroundColor: "var(--warn)", display: "inline-block" }} />
      Lightbulb&apos;s background service is off
      <button type="button" style={{ textDecoration: "underline" }} onClick={() => void Promise.resolve(bridge()?.("engine_fix")).catch(() => undefined)}>Fix</button>
    </div>
  );
}
