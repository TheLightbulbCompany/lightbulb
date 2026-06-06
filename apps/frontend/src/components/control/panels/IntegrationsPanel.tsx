"use client";

import { useState } from "react";
import { useOrganization } from "@clerk/nextjs";
import { ChevronDown } from "lucide-react";

import { ConnectionsList } from "@/components/settings/ConnectionsList";
import { GoogleAccountsSection } from "@/components/control/panels/GoogleAccountsSection";
import { McpServersTab } from "@/components/control/panels/McpServersTab";
import { useConnections } from "@/hooks/useConnections";
import { cn } from "@/lib/utils";

/**
 * Control-dashboard home for third-party integrations. This re-homes the
 * same connect/disconnect surface that powers /settings/connections — it
 * reuses ConnectionsList (+ ConnectButton + useConnections) as-is, so
 * paste-token providers connect inline here and the shared /connections
 * SWR cache stays in sync across both surfaces.
 *
 * OAuth providers still redirect out to /settings/connections on return
 * (the backend's fixed callback URL). That's acceptable: the user lands
 * back in Settings with the row already flipped to connected, and this
 * tab picks up the same cached status the next time it mounts.
 */
export function IntegrationsPanel() {
  const { membership } = useOrganization();

  // A personal owner has no org membership and is implicitly the admin of
  // their own resources; only non-admin members of a real org are gated.
  // This is a UX hint only — the connect/disconnect endpoints enforce the
  // same rule server-side (403), which ConnectButton surfaces inline.
  const canManage = !membership || membership.role === "org:admin";

  const [mcpOpen, setMcpOpen] = useState(false);

  // The Google multi-account surface only makes sense once Google is
  // connected. We read the same shared /connections cache ConnectionsList
  // uses (no extra request) to decide whether to render it. The provider
  // list is server-driven, so this is a no-op until a `google` row exists.
  const { connections } = useConnections();
  const googleConnected = connections.some(
    (c) => c.provider === "google" && c.status === "connected",
  );

  return (
    <div className="bg-[#faf7f2] min-h-full">
      <div className="p-6 space-y-5">
        <div>
          <h2 className="text-xl font-semibold" style={{ fontFamily: "'Lora', serif" }}>
            Integrations
          </h2>
          <p
            className="text-sm text-[#8a8578] mt-0.5"
            style={{ fontFamily: "'DM Sans', sans-serif" }}
          >
            Connect third-party tools so your agents can act on your behalf. API-key
            integrations take a token you paste; OAuth integrations send you to the provider
            to sign in.
          </p>
        </div>

        {!canManage && (
          <div className="rounded-md border border-[#e0dbd0] bg-white p-4 text-sm text-[#8a8578]">
            Only organization admins can manage integrations.
          </div>
        )}

        <ConnectionsList canManage={canManage} />

        {googleConnected && <GoogleAccountsSection canManage={canManage} />}

        {/* Advanced: the raw mcp.servers escape hatch. Curated integrations
            above write the same mcp.servers config; this is the manual rail
            for MCP servers that don't have a curated card. McpServersTab
            self-gates non-admins (403 -> "org admins only"). */}
        <div className="rounded-md border border-[#e0dbd0] bg-white">
          <button
            type="button"
            aria-expanded={mcpOpen}
            onClick={() => setMcpOpen((v) => !v)}
            className="flex w-full items-center justify-between p-4 text-left"
          >
            <span className="text-sm font-medium text-[#1a1a1a]" style={{ fontFamily: "'DM Sans', sans-serif" }}>
              Advanced — raw MCP servers
            </span>
            <ChevronDown className={cn("h-4 w-4 text-[#8a8578] transition-transform", !mcpOpen && "-rotate-90")} />
          </button>
          {mcpOpen && (
            <div className="border-t border-[#e0dbd0]">
              <McpServersTab />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
