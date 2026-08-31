"use client";

import "../settings.css";
import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { UserButton } from "@clerk/nextjs";

import { Logo } from "@/components/Logo";
import { ConnectionsList } from "@/components/settings/ConnectionsList";
import { StripeSection } from "@/components/settings/StripeSection";
import { GoogleAccountsSection } from "@/components/control/panels/GoogleAccountsSection";
import { useConnections } from "@/hooks/useConnections";
import { SettingsNav } from "@/components/settings/SettingsNav";
import { SETTINGS_NAV_SECTIONS } from "@/components/settings/settings-nav-config";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";

/**
 * /settings/connections — manage third-party integrations.
 *
 * This is a real route (not a ?panel= tab on /settings) because the OAuth
 * providers redirect the browser back to /settings/connections?connected=…
 * after consent. A standalone route gives that redirect a stable landing
 * URL and lets this page own the return handling. It re-uses the settings
 * chrome (topbar + left nav) so it reads as part of Settings.
 */
export default function ConnectionsPage() {
  // useSearchParams() forces dynamic rendering; the Suspense boundary keeps
  // the static-export step happy, matching /settings/page.tsx.
  return (
    <Suspense fallback={null}>
      <ConnectionsPageInner />
    </Suspense>
  );
}

type Banner = { kind: "success" | "error"; message: string } | null;

function oauthReturnBanner(
  connected: string | null,
  error: string | null,
  stripe: string | null,
  stripeConnect: string | null,
  stripeError: string | null,
): Banner {
  if (connected) {
    return { kind: "success", message: `Connected ${connected}.` };
  }
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
  if (stripeConnect === "connected") {
    return { kind: "success", message: "Connected your Stripe account." };
  }
  if (stripe === "return") {
    return { kind: "success", message: "Stripe onboarding finished — status updates in a moment." };
  }
  // stripe === "refresh" means the hosted onboarding link expired; no banner —
  // the section's "Finish onboarding" button mints a fresh one.
  if (stripeError) {
    return { kind: "error", message: "Couldn't connect Stripe. Please try again." };
  }
  return null;
}

function ConnectionsPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  // Same shared nav as /settings (the 6 intent-ordered groups). Connections is
  // highlighted via activeHref.
  const navSections = SETTINGS_NAV_SECTIONS;
  const isAdmin = useIsWorkspaceAdmin();

  const canManage = isAdmin;

  const { connections, mutate } = useConnections();
  const [banner, setBanner] = useState<Banner>(null);

  // Google is the one provider that holds MANY accounts behind a single
  // connection row, and this page is where its own "connect another" OAuth
  // round trip LANDS — so the multi-account surface has to be here. It used to
  // live only in the Control dashboard's Integrations tab, which meant the
  // page called "Connections" showed a connected Google as a single account
  // with nothing but a Disconnect button.
  const googleConnected = connections.some(
    (c) => c.provider === "google" && c.status === "connected",
  );

  // OAuth return handling. The provider redirects here with ?connected= or
  // ?error=; we surface a banner, refresh the list so the freshly-connected
  // provider flips to green, then strip the query param from the URL so a
  // refresh / back-nav doesn't replay the banner (mirrors useChatGPTOAuth).
  useEffect(() => {
    const connected = searchParams.get("connected");
    const error = searchParams.get("error");
    const stripe = searchParams.get("stripe");
    const stripeConnect = searchParams.get("stripe_connect");
    const stripeError = searchParams.get("stripe_error");
    if (!connected && !error && !stripe && !stripeConnect && !stripeError) return;

    // Reflecting the OAuth-return URL params into a one-shot banner — an
    // external-data→React sync the rule sanctions. We immediately strip the
    // query param below so this only fires once per redirect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setBanner(oauthReturnBanner(connected, error, stripe, stripeConnect, stripeError));
    if (connected) void mutate();
    router.replace("/settings/connections");
  }, [searchParams, router, mutate]);

  return (
    <>
      <header
        className="settings-topbar"
        style={{ fontFamily: "var(--font-sans)" }}
      >
        <div className="settings-topbar-left">
          <Logo size="md" href="/chat" />
          <nav className="settings-topbar-breadcrumb" aria-label="Breadcrumb">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <polyline points="9 18 15 12 9 6" />
            </svg>
            <Link href="/settings" className="settings-current" style={{ textDecoration: "none", color: "var(--midnight-soft)", fontWeight: 500 }}>
              Settings
            </Link>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <polyline points="9 18 15 12 9 6" />
            </svg>
            <span className="settings-current" aria-current="page">Connections</span>
          </nav>
        </div>
        <div className="settings-topbar-right">
          <Link href="/chat" className="settings-btn-back-chat">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M19 12H5" />
              <polyline points="12 19 5 12 12 5" />
            </svg>
            Back to Chat
          </Link>
          <UserButton afterSignOutUrl="/" />
        </div>
      </header>

      <div
        className="settings-page-wrap"
        style={{ fontFamily: "var(--font-sans)" }}
      >
        <div className="settings-layout">
          <SettingsNav
            mode="links"
            sections={navSections}
            isAdmin={isAdmin}
            activeHref="/settings/connections"
          />

          <main className="settings-main">
            <div className="settings-panel" role="tabpanel">
              <h1 className="settings-page-title">Connections</h1>
              <p className="settings-page-desc">
                Connect third-party tools so your agents can act on your behalf. API-key
                integrations take a token you paste; OAuth integrations send you to the
                provider to sign in.
              </p>

              {banner && (
                <div
                  className={`settings-alert ${banner.kind === "success" ? "settings-alert-success" : "settings-alert-error"}`}
                  role="status"
                >
                  {banner.message}
                </div>
              )}

              {!canManage && (
                <div className="settings-alert settings-alert-error" role="status">
                  Only organization admins can manage connections.
                </div>
              )}

              {googleConnected && (
                <div className="mb-4">
                  <GoogleAccountsSection canManage={canManage} />
                </div>
              )}

              {/* Stripe is ONE brand with TWO separate grants (company account
                  + agent tools), so it gets a consolidated section here and is
                  excluded from the generic list below. */}
              <div className="mb-4">
                <StripeSection canManage={canManage} />
              </div>

              <ConnectionsList canManage={canManage} exclude={["stripe"]} />
            </div>
          </main>
        </div>
      </div>
    </>
  );
}
