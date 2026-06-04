"use client";

import { useOrganization } from "@clerk/nextjs";

import { ConnectionsList } from "@/components/settings/ConnectionsList";
import { GoogleAccountsSection } from "@/components/control/panels/GoogleAccountsSection";
import { useConnections } from "@/hooks/useConnections";

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
      </div>
    </div>
  );
}
