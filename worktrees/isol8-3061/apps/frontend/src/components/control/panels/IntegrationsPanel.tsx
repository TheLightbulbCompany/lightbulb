"use client";

import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";

import { ConnectionsList } from "@/components/settings/ConnectionsList";
import { GoogleAccountsSection } from "@/components/control/panels/GoogleAccountsSection";
import { McpServersTab } from "@/components/control/panels/McpServersTab";
import { useConnections } from "@/hooks/useConnections";

/**
 * Control-dashboard home for third-party integrations. This re-homes the
 * same connect/disconnect surface that powers the rail's Connections panel —
 * it reuses ConnectionsList (+ ConnectButton + useConnections) as-is, so
 * paste-token providers connect inline here and the shared /connections
 * SWR cache stays in sync across both surfaces.
 *
 * OAuth providers still redirect out to the backend's fixed callback URL on
 * return, which lands the user on the Connections panel rather than back
 * here. That's acceptable: they land with the row already flipped to
 * connected, and this tab picks up the same cached status when it remounts.
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

  return (
    <div className="bg-[var(--cream)] min-h-full">
      <div className="p-6 space-y-5">
        <div>
          <h2 className="font-editorial-serif text-title">
            Integrations
          </h2>
          <p
            className="text-sm text-[var(--midnight-muted)] mt-0.5"
          >
            Connect third-party tools so your agents can act on your behalf. API-key
            integrations take a token you paste; OAuth integrations send you to the provider
            to sign in.
          </p>
        </div>

        {!canManage && (
          <div className="rounded-md border border-[var(--edge)] bg-cream p-4 text-sm text-[var(--midnight-muted)]">
            Only organization admins can manage integrations.
          </div>
        )}

        {/* Above the provider catalog, not below it. The catalog is ~15 cards
            of things you might connect; this is the state of something you
            already did, and the only place multi-account Google is managed.
            Below the fold it reads as absent — which is how an owner ends up
            concluding one Google account is all there is. */}
        {googleConnected && <GoogleAccountsSection canManage={canManage} />}

        <ConnectionsList canManage={canManage} />

        {/* Bring-your-own MCP servers. A mainstream act now (vendors hand out
            remote MCP URLs), so it sits in the open — not behind Advanced.
            Curated integrations above write the same mcp.servers config; this is
            the manual rail. McpServersTab self-gates non-admins (403). */}
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
