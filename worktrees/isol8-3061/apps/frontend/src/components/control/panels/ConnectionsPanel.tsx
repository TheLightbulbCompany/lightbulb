"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { ConnectionsList } from "@/components/settings/ConnectionsList";
import { StripeSection } from "@/components/settings/StripeSection";
import { GoogleAccountsSection } from "@/components/control/panels/GoogleAccountsSection";
import { useConnections } from "@/hooks/useConnections";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";

/**
 * Connections — the workspace rail's third-party connect/disconnect surface,
 * sitting under Browser and Files.
 *
 * This used to be the /settings/connections PAGE. It moved into the dock so
 * connecting a tool reads as part of the workspace rather than a trip into
 * Settings, and the Settings nav no longer carries the row. The route still
 * exists as a redirector into this panel, because it is the fixed landing URL
 * for OAuth consent AND for the backend's `STRIPE_COMPANY_RETURN_URL` — so the
 * return params arrive here, on /chat, and this panel owns them.
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
    <div className="bg-[var(--cream)] min-h-full">
      <div className="p-6 space-y-5">
        <div>
          <h2 className="font-editorial-serif text-title">Connections</h2>
          <p className="text-sm text-[var(--midnight-muted)] mt-0.5">
            Connect third-party tools so your agents can act on your behalf. API-key
            integrations take a token you paste; OAuth integrations send you to the
            provider to sign in.
          </p>
        </div>

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

        <ConnectionsList canManage={canManage} exclude={["stripe"]} />
      </div>
    </div>
  );
}

type Banner = { kind: "success" | "error"; message: string } | null;

/** The five params an OAuth or Stripe return can land on /chat carrying. They
 *  are in the registry's NON_COMMAND_PARAMS, so stripping them below never
 *  churns the workspace reconciler's command signature. */
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
    router.replace(query ? `/chat?${query}` : "/chat");
  }, [search, router, onConnected]);

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
