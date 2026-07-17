"use client";

import { useState } from "react";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { ChevronDown } from "lucide-react";

import { ConnectionsList } from "@/components/settings/ConnectionsList";
import { GoogleAccountsSection } from "@/components/control/panels/GoogleAccountsSection";
import { McpServersTab } from "@/components/control/panels/McpServersTab";
import { PluginsTab } from "@/components/control/panels/PluginsTab";
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
interface IntegrationsPanelProps {
  /**
   * Where this panel is mounted. The Control dashboard ("control", default)
   * keeps the panel's own full-bleed `#faf7f2` frame + `p-6` padding + the
   * Lora "Integrations" heading. Mounted under /settings ("settings") it
   * renders inside the shared `.settings-panel` frame with the standard
   * `.settings-page-title` / `.settings-page-desc`, so MCP servers reads as
   * the same surface as the other Settings panels (.settings-main already
   * supplies the page padding — no extra bg/padding here).
   */
  variant?: "control" | "settings";
}

export function IntegrationsPanel({ variant = "control" }: IntegrationsPanelProps) {
  const isSettings = variant === "settings";

  // UX hint only — the connect/disconnect endpoints enforce the same rule
  // server-side (403), which ConnectButton surfaces inline.
  const canManage = useIsWorkspaceAdmin();

  const [advancedOpen, setAdvancedOpen] = useState(false);

  // The Google multi-account surface only makes sense once Google is
  // connected. We read the same shared /connections cache ConnectionsList
  // uses (no extra request) to decide whether to render it. The provider
  // list is server-driven, so this is a no-op until a `google` row exists.
  const { connections } = useConnections();
  const googleConnected = connections.some(
    (c) => c.provider === "google" && c.status === "connected",
  );

  // Settings mount: the shared `.settings-panel` frame. .settings-main already
  // provides the page padding, so no extra bg / min-height / p-6 here, and the
  // heading uses the .settings-page-title/.settings-page-desc idiom the other
  // Settings panels use — so MCP servers doesn't read as a different page.
  if (isSettings) {
    return (
      <div className="settings-panel" id="panel-mcp" role="tabpanel">
        <h1 className="settings-page-title">MCP servers</h1>
        <p className="settings-page-desc">
          Register Model Context Protocol servers your agents can call — paste a
          server config below, or open Advanced for plugins. To connect third-party
          apps like Stripe, Google, or Notion, use Connections.
        </p>

        {!canManage && (
          <div className="settings-alert settings-alert-error" role="status">
            Only organization admins can manage MCP servers.
          </div>
        )}

        <div className="space-y-5">
          <div className="space-y-2">
            <h3 className="text-sm font-medium text-[#1a1a1a]" style={{ fontFamily: "'DM Sans', sans-serif" }}>
              Add your own MCP servers
            </h3>
            <div className="rounded-md border border-[#e0dbd0] bg-white">
              <McpServersTab />
            </div>
          </div>

          <div className="rounded-md border border-[#e0dbd0] bg-white">
            <button
              type="button"
              aria-expanded={advancedOpen}
              onClick={() => setAdvancedOpen((v) => !v)}
              className="flex w-full items-center justify-between p-4 text-left"
            >
              <span className="text-sm font-medium text-[#1a1a1a]" style={{ fontFamily: "'DM Sans', sans-serif" }}>
                Advanced — plugins
              </span>
              <ChevronDown className={cn("h-4 w-4 text-[#8a8578] transition-transform", !advancedOpen && "-rotate-90")} />
            </button>
            {advancedOpen && (
              <div className="border-t border-[#e0dbd0]">
                <PluginsTab />
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

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

        {/* Bring-your-own MCP servers. A mainstream act now (vendors hand out
            remote MCP URLs), so it sits in the open — not behind Advanced.
            Curated integrations above write the same mcp.servers config; this is
            the manual rail. McpServersTab self-gates non-admins (403). */}
        <div className="space-y-2">
          <h3 className="text-sm font-medium text-[#1a1a1a]" style={{ fontFamily: "'DM Sans', sans-serif" }}>
            Add your own MCP servers
          </h3>
          <div className="rounded-md border border-[#e0dbd0] bg-white">
            <McpServersTab />
          </div>
        </div>

        {/* Advanced: plugin enable/disable switches. Plugins run in-process with
            the agent, which is why this — unlike MCP — keeps a wall. Channel and
            provider plugins get real homes elsewhere (Channels / Model & Credits). */}
        <div className="rounded-md border border-[#e0dbd0] bg-white">
          <button
            type="button"
            aria-expanded={advancedOpen}
            onClick={() => setAdvancedOpen((v) => !v)}
            className="flex w-full items-center justify-between p-4 text-left"
          >
            <span className="text-sm font-medium text-[#1a1a1a]" style={{ fontFamily: "'DM Sans', sans-serif" }}>
              Advanced — plugins
            </span>
            <ChevronDown className={cn("h-4 w-4 text-[#8a8578] transition-transform", !advancedOpen && "-rotate-90")} />
          </button>
          {advancedOpen && (
            <div className="border-t border-[#e0dbd0]">
              <PluginsTab />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
