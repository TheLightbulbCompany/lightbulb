"use client";

import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { McpServersTab } from "@/components/control/panels/McpServersTab";

/**
 * Settings → Advanced → MCP servers: register Model Context Protocol servers
 * the agents can call. The provider catalog it used to repeat as the dock's
 * Integrations tab lives once, in Settings → Connections.
 */
export function IntegrationsPanel() {
  // UX hint only — McpServersTab self-gates non-admins (403).
  const canManage = useIsWorkspaceAdmin();

  return (
    <div className="settings-panel" id="panel-mcp" role="tabpanel">
      <h1 className="settings-page-title">MCP servers</h1>
      <p className="settings-page-desc">
        Register Model Context Protocol servers your agents can call — paste a
        server config below. To connect third-party apps like Stripe, Google,
        or Notion, use Connections.
      </p>

      {!canManage && (
        <div className="settings-alert settings-alert-error" role="status">
          Only organization admins can manage MCP servers.
        </div>
      )}

      <div className="space-y-5">
        <div className="space-y-2">
          <h3 className="text-sm font-medium text-[var(--midnight)]">
            Add your own MCP servers
          </h3>
          <div className="rounded-md border border-[var(--edge)] bg-cream">
            <McpServersTab />
          </div>
        </div>
      </div>
    </div>
  );
}
