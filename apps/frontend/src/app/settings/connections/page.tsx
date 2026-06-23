"use client";

import "../settings.css";
import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { useOrganization, UserButton } from "@clerk/nextjs";

import { Logo } from "@/components/Logo";
import { ConnectionsList } from "@/components/settings/ConnectionsList";
import { useConnections } from "@/hooks/useConnections";
import { SettingsNav } from "@/components/settings/SettingsNav";
import {
  LEGACY_NAV_SECTIONS,
  UNIFIED_NAV_SECTIONS,
} from "@/components/settings/settings-nav-config";
import { useIsControlAdmin } from "@/components/control/ControlSidebar";
import { useUnifiedNav } from "@/hooks/useUnifiedNav";

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

function oauthReturnBanner(connected: string | null, error: string | null): Banner {
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
  return null;
}

function ConnectionsPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { membership } = useOrganization();

  // Same flag + admin gate as /settings so the shared nav renders the matching
  // group set: flag-OFF shows the legacy single "Account" group, flag-ON shows
  // the 6 intent-ordered groups. Connections is highlighted via activeHref.
  const unifiedNav = useUnifiedNav();
  const navSections = unifiedNav ? UNIFIED_NAV_SECTIONS : LEGACY_NAV_SECTIONS;
  const isAdmin = useIsControlAdmin();

  // A personal owner has no org membership and is implicitly the admin of
  // their own resources; only restrict non-admin members of a real org.
  const canManage = !membership || membership.role === "org:admin";

  const { mutate } = useConnections();
  const [banner, setBanner] = useState<Banner>(null);

  // OAuth return handling. The provider redirects here with ?connected= or
  // ?error=; we surface a banner, refresh the list so the freshly-connected
  // provider flips to green, then strip the query param from the URL so a
  // refresh / back-nav doesn't replay the banner (mirrors useChatGPTOAuth).
  useEffect(() => {
    const connected = searchParams.get("connected");
    const error = searchParams.get("error");
    if (!connected && !error) return;

    // Reflecting the OAuth-return URL params into a one-shot banner — an
    // external-data→React sync the rule sanctions. We immediately strip the
    // query param below so this only fires once per redirect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setBanner(oauthReturnBanner(connected, error));
    if (connected) void mutate();
    router.replace("/settings/connections");
  }, [searchParams, router, mutate]);

  return (
    <>
      <header
        className="settings-topbar"
        style={{ fontFamily: "var(--font-dm-sans), 'DM Sans', sans-serif" }}
      >
        <div className="settings-topbar-left">
          <Logo size="md" />
          <nav className="settings-topbar-breadcrumb" aria-label="Breadcrumb">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <polyline points="9 18 15 12 9 6" />
            </svg>
            <Link href="/settings" className="settings-current" style={{ textDecoration: "none", color: "#59534d", fontWeight: 500 }}>
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
        style={{ fontFamily: "var(--font-dm-sans), 'DM Sans', sans-serif" }}
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

              <ConnectionsList canManage={canManage} />
            </div>
          </main>
        </div>
      </div>
    </>
  );
}
