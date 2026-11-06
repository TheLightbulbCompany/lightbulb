"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { ConnectionsList } from "@/components/settings/ConnectionsList";
import { StripeSection } from "@/components/settings/StripeSection";
import { GoogleAccountsSection } from "@/components/control/panels/GoogleAccountsSection";
import { WebsiteLoginsSection } from "@/components/control/panels/WebsiteLoginsSection";
import { BrowserExtensionsSection } from "@/components/control/panels/BrowserExtensionsSection";
import { AgentKeysSection } from "@/components/control/panels/AgentKeysSection";
import { LinkWalletSection } from "@/components/control/panels/LinkWalletSection";
import { useConnections } from "@/hooks/useConnections";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";

/**
 * Settings → Connections: one list of what the team is connected to, and what
 * its agents may do with it. It absorbed the dock's Integrations and Accounts
 * tabs (2026-10-14); the rail's Connections button opens it.
 *
 * /settings/connections redirects here, because it is the fixed landing URL for
 * OAuth consent AND for the backend's `STRIPE_COMPANY_RETURN_URL` — so the
 * return params arrive here and this panel owns them.
 */
export function ConnectionsPanel() {
  // UX hint only — connect/disconnect is enforced server-side (403), which
  // ConnectButton surfaces inline.
  const canManage = useIsWorkspaceAdmin();

  const { connections, mutate } = useConnections();

  // Google is the one provider that holds MANY accounts behind a single
  // connection row, and this is where its own "connect another" OAuth round
  // trip lands — so the multi-account surface has to be here. Reads the shared
  // /connections cache ConnectionsList already uses, so no extra request.
  const googleConnected = connections.some(
    (c) => c.provider === "google" && c.status === "connected",
  );

  return (
    <div className="settings-panel" id="panel-connections" role="tabpanel">
      <h1 className="settings-page-title">Connections</h1>
      <p className="settings-page-desc">
        Connect third-party tools so your agents can act on your behalf. API-key
        integrations take a token you paste; OAuth integrations send you to the
        provider to sign in.
      </p>
      <div className="space-y-5">

        <ConnectionsReturnBanner onConnected={mutate} />

        {!canManage && (
          <div className="rounded-md border border-[var(--edge)] bg-cream p-4 text-sm text-[var(--midnight-muted)]" role="status">
            Only organization admins can manage connections.
          </div>
        )}

        {/* Above the provider catalog, not below it — the catalog is things you
            might connect; this is the state of something you already did. */}
        {googleConnected && <GoogleAccountsSection canManage={canManage} />}

        {/* Stripe is ONE brand with TWO separate grants (company account +
            agent tools), so it gets a consolidated section here and is
            excluded from the generic list below. */}
        <StripeSection canManage={canManage} />
        <LinkWalletSection canManage={canManage} />

        {/* Sign-ins the shared browser fills for agents (the Browser panel's menu opens here). */}
        <WebsiteLoginsSection canManage={canManage} />
        <BrowserExtensionsSection />
        <AgentKeysSection />

        <ConnectionsList canManage={canManage} exclude={["stripe"]} />

      </div>
    </div>
  );
}

type Banner = { kind: "success" | "error"; message: string } | null;

/** The five params an OAuth or Stripe return can land on Settings carrying. */
const RETURN_PARAMS = ["connected", "error", "stripe", "stripe_connect", "stripe_error"] as const;

export function returnBannerFor(params: URLSearchParams): Banner {
  const connected = params.get("connected");
  const error = params.get("error");

  if (connected) return { kind: "success", message: `Connected ${connected}.` };
  if (error) {
    const message =
      error === "oauth_state"
        ? "We couldn't verify the sign-in. Please try connecting again."
        : error === "oauth_exchange"
          ? "Sign-in failed. Double-check your app's client ID and secret, then try again."
          : error === "apply_failed"
            ? "We connected, but couldn't apply the credential. Please try again."
            : "Connection failed. Please try again.";
    return { kind: "error", message };
  }
  if (params.get("stripe_connect") === "connected") {
    return { kind: "success", message: "Connected your Stripe account." };
  }
  if (params.get("stripe") === "return") {
    return { kind: "success", message: "Stripe onboarding finished — status updates in a moment." };
  }
  // stripe === "refresh" means the hosted onboarding link expired; no banner —
  // StripeSection's "Finish onboarding" button mints a fresh one.
  if (params.get("stripe_error")) {
    return { kind: "error", message: "Couldn't connect Stripe. Please try again." };
  }
  return null;
}

/**
 * One-shot banner for an OAuth / Stripe return, then the strip.
 *
 * Split out of the panel so the effect owns exactly one concern: read the
 * return params, show the outcome once, refresh the list, and take the params
 * back out of the URL so a refresh or a Back nav can't replay the banner. The
 * strip preserves every other param (`?panel=connections` included).
 */
function ConnectionsReturnBanner({ onConnected }: { onConnected: () => void }) {
  const router = useRouter();
  const pathname = usePathname();
  // The STRING, not the object: useSearchParams() returns a fresh instance on
  // every render, so an object dep would re-run this effect on the very
  // re-render its own setBanner causes — an unbounded loop that exhausts the
  // heap. The string only changes when the URL does.
  const search = useSearchParams().toString();
  const [banner, setBanner] = useState<Banner>(null);

  useEffect(() => {
    const params = new URLSearchParams(search);
    if (!RETURN_PARAMS.some((k) => params.get(k))) return;

    // Reflecting the return URL into a one-shot banner — an external-data→React
    // sync the rule sanctions. The strip below means this fires once per return.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setBanner(returnBannerFor(params));
    if (params.get("connected")) onConnected();

    for (const k of RETURN_PARAMS) params.delete(k);
    const query = params.toString();
    router.replace(query ? `${pathname}?${query}` : pathname);
  }, [search, router, pathname, onConnected]);

  if (!banner) return null;
  return (
    <div
      className={`rounded-md border bg-cream p-4 text-sm ${
        banner.kind === "success"
          ? "border-[var(--ok)] text-[var(--ok)]"
          : "border-[var(--err)] text-[var(--err)]"
      }`}
      role="status"
    >
      {banner.message}
    </div>
  );
}
