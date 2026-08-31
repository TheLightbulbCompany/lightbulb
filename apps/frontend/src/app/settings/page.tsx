"use client";

import "./settings.css";
import React, { Suspense, useEffect, useState, useCallback } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";

import { capture } from "@/lib/analytics";
import { planLabel } from "@/lib/planLabel";
import { useAuth, useUser, UserButton, useOrganization, useClerk } from "@clerk/nextjs";
import { useApi, ApiError } from "@/lib/api";
import { useBilling } from "@/hooks/useBilling";
import { ProviderPicker } from "@/components/chat/ProvisioningStepper";
import { MyChannelsSection } from "@/components/settings/MyChannelsSection";
import { WebhooksPanel } from "@/components/settings/WebhooksPanel";
import { DesktopFileAccess } from "@/components/desktop/DesktopFileAccess";
import { HandlePicker } from "@/components/marketplace/HandlePicker";
import { getMySellerProfile, type SellerProfile } from "@/lib/marketplace/api";
import { Logo } from "@/components/Logo";
import { TeamBillingCard } from "@/components/chat/TeamBillingCard";
import { UsagePanel } from "@/components/control/panels/UsagePanel";
import { LLMPanel } from "@/components/control/panels/LLMPanel";
import { IntegrationsPanel } from "@/components/control/panels/IntegrationsPanel";
import { SkillsTab } from "@/components/control/panels/SkillsTab";
import { SkillProposalsPanel } from "@/components/control/panels/SkillProposalsPanel";
import { ChannelsStatusPanel } from "@/components/control/panels/ChannelsStatusPanel";
import { ADMIN_ONLY_PANELS } from "@/components/control/ControlSidebar";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { SettingsNav } from "@/components/settings/SettingsNav";
import {
  SETTINGS_NAV_SECTIONS,
  type NavItem,
} from "@/components/settings/settings-nav-config";

// =============================================================================
// Types & Navigation
//
// The nav sections, the NavItem type, and NavIcon live in the shared
// settings-nav-config module so /settings and /settings/connections render
// the identical sidebar from ONE source. This page re-imports the section
// consts + NavItem above; the connections route imports the same module via
// <SettingsNav>.
// =============================================================================

type Panel =
  | "profile"
  | "billing"
  | "usage"
  | "providers"
  | "channels"
  | "mcp"
  | "webhooks"
  | "desktop"
  | "skills"
  | "skill-proposals"
  | "channel-status"
  | "marketplace";

// =============================================================================
// Profile Panel
// =============================================================================

const DANGER = "var(--err)";

// Self-serve account deletion (mirrors the mobile settings flow): explain
// permanence, require the account email typed back (same guard the backend
// enforces), run the server-side teardown, then sign out. Org members get the
// support-only message — workspace deletion has per-member fan-out the wipe
// orchestrator refuses, so the backend 403s it.
function DeleteAccountCard() {
  const { user } = useUser();
  // `isLoaded` is load-bearing (same window FirstRunFunnel documents): Clerk
  // resolves the org context independently, so `organization` can be unset
  // while a workspace membership is still loading. Reading that as "personal"
  // would offer the deletion flow to an org member, whose request the backend
  // 403s. Until it resolves, render nothing.
  const { organization, isLoaded: orgLoaded } = useOrganization();
  const { signOut } = useClerk();
  const api = useApi();

  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const email = user?.primaryEmailAddress?.emailAddress ?? "";

  const handleDelete = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post("/users/me/delete", { confirm_email: typed.trim() });
      // The Clerk user is already gone server-side (last teardown step);
      // signOut just clears the local session. If it throws, hard-redirect.
      await signOut({ redirectUrl: "/" }).catch(() => {
        window.location.href = "/";
      });
    } catch (err) {
      const detail =
        err instanceof ApiError && typeof (err.body as { detail?: unknown })?.detail === "string"
          ? (err.body as { detail: string }).detail
          : null;
      setError(
        detail === "email_mismatch"
          ? "That email didn't match this account."
          : detail === "deletion_incomplete"
            ? "Deletion didn't finish — try again in a moment, or contact support@isol8.co."
            : (detail ?? "Something went wrong — try again or contact support@isol8.co."),
      );
      setBusy(false);
    }
  }, [api, typed, signOut]);

  if (!orgLoaded) return null;

  return (
    <div className="settings-card" style={{ borderColor: "color-mix(in srgb, var(--err) 25%, transparent)" }}>
      <div className="settings-card-title" style={{ color: DANGER }}>Delete account</div>
      {organization ? (
        <div className="settings-card-desc">
          Workspace accounts can&apos;t be deleted in-app — contact support@isol8.co.
        </div>
      ) : !open ? (
        <>
          <div className="settings-card-desc">
            Permanently delete your account, agents, workspace, and data. This cannot be undone.
          </div>
          <button
            type="button"
            className="settings-btn-outline"
            style={{ color: DANGER, borderColor: "color-mix(in srgb, var(--err) 40%, transparent)" }}
            onClick={() => setOpen(true)}
          >
            Delete account…
          </button>
        </>
      ) : (
        <>
          <div className="settings-card-desc">
            Type your account email ({email}) to confirm. This permanently deletes your account,
            agents, workspace, and data — it cannot be undone.
          </div>
          <input
            type="email"
            className="settings-overage-input"
            style={{ width: "100%", maxWidth: 360, marginBottom: 10 }}
            placeholder={email}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            disabled={busy}
            aria-label="Confirm account email"
          />
          <div style={{ display: "flex", gap: 8 }}>
            <button
              type="button"
              className="settings-btn-outline"
              style={{ color: "var(--cream)", background: DANGER, borderColor: DANGER }}
              onClick={handleDelete}
              disabled={busy || typed.trim().toLowerCase() !== email.trim().toLowerCase()}
            >
              {busy && <span className="settings-spinner" style={{ width: 14, height: 14, borderWidth: 2, marginRight: 6, display: "inline-block", verticalAlign: "middle" }} />}
              Delete forever
            </button>
            <button
              type="button"
              className="settings-btn-outline"
              onClick={() => { setOpen(false); setTyped(""); setError(null); }}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
          {error && (
            <div className="settings-alert settings-alert-error" style={{ marginTop: 10 }}>
              {error}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function ProfilePanel() {
  const { user } = useUser();
  const { account } = useBilling();
  const subscriptionStatus = account?.subscription_status ?? null;
  const isTrialLocal = subscriptionStatus === "trial_local";

  const email = user?.primaryEmailAddress?.emailAddress ?? "";
  const initials = `${(user?.firstName ?? "")[0] ?? ""}${(user?.lastName ?? "")[0] ?? ""}`.toUpperCase();

  return (
    <div className="settings-panel" id="panel-profile" role="tabpanel">
      <h1 className="settings-page-title">Profile</h1>
      <p className="settings-page-desc">Your account information, managed by your authentication provider.</p>

      <div className="settings-card">
        <div className="settings-profile-row">
          <div className="settings-profile-avatar-lg" aria-hidden="true">{initials}</div>
          <div>
            <div className="settings-profile-name">
              {user?.fullName ?? "User"}
              {planLabel(subscriptionStatus) && (
                // .settings-profile-badge is text-transform: uppercase
                // (settings.css) — fine for "ACTIVE" but would shout "FREE
                // TIER". Override just the transform for the mapped label,
                // same technique as the billing chip below.
                <span
                  className="settings-profile-badge"
                  style={{ textTransform: isTrialLocal ? "none" : undefined }}
                >
                  {planLabel(subscriptionStatus)}
                </span>
              )}
            </div>
            <div className="settings-profile-email">{email}</div>
          </div>
        </div>
        <div className="settings-card-desc" style={{ marginTop: 0 }}>
          To update your name, email, or password, use the account menu in the top-right corner of the app.
        </div>
      </div>

      <DeleteAccountCard />
    </div>
  );
}

// =============================================================================
// Billing Panel
// =============================================================================

// Keep in lockstep with STRIPE_ULTRA_PRICE_ID's unit amount in Stripe. The
// billing account response carries only `plan`, not the price, so the display
// price is a labeled constant here rather than echoed from the server.
const ULTRA_PRICE_USD = 150;

// Ultra Pro control for a non-per-seat subscribed owner. When the owner is
// already on ultra_pro it collapses to an "Ultra Pro" active badge; otherwise
// it renders the upgrade button that calls useBilling().upgradeToUltra (which
// redirects to a fresh Checkout, or refreshes in place on the item-swap path).
// Never rendered on the per-seat TeamBillingCard — Ultra-for-teams exists at
// the billing layer, but the org upgrade CTA hasn't been built on the
// frontend yet.
function UltraUpgradeRow({
  plan,
  upgradeToUltra,
}: {
  plan: "standard" | "ultra_pro" | undefined;
  upgradeToUltra: () => Promise<void>;
}) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleUpgrade = useCallback(async () => {
    capture("billing_ultra_upgrade_clicked");
    setError(null);
    setLoading(true);
    try {
      await upgradeToUltra();
      // The fresh-checkout path redirects away and never returns here; the
      // in-place swap resolves and the refreshed account re-renders this row
      // as the Ultra badge — clear the spinner either way.
      setLoading(false);
    } catch {
      setError("Couldn't start the upgrade. Please try again.");
      setLoading(false);
    }
  }, [upgradeToUltra]);

  if (plan === "ultra_pro") {
    return (
      <div style={{ marginTop: 14 }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, fontWeight: 600, color: "var(--midnight)", background: "color-mix(in srgb, var(--midnight) 8%, transparent)", padding: "3px 10px", borderRadius: 999 }}>
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
          Ultra Pro
        </span>
        <div className="settings-card-desc" style={{ marginTop: 6 }}>
          Larger marketplace companies unlocked.
        </div>
      </div>
    );
  }

  return (
    <div style={{ marginTop: 14 }}>
      <button type="button" className="settings-btn-save" onClick={handleUpgrade} disabled={loading}>
        {loading && <span className="settings-spinner" style={{ width: 14, height: 14, borderWidth: 2, marginRight: 6, display: "inline-block", verticalAlign: "middle" }} />}
        Upgrade to Ultra Pro — ${ULTRA_PRICE_USD}/mo
      </button>
      {error && (
        <div className="settings-alert settings-alert-error" style={{ marginTop: 8 }}>
          {error}
        </div>
      )}
    </div>
  );
}

function BillingPanel() {
  const {
    account,
    isLoading,
    error: accountError,
    isSubscribed,
    openPortal,
    startTeamCheckout,
    upgradeToUltra,
  } = useBilling();
  const { membership } = useOrganization();

  const [portalLoading, setPortalLoading] = useState(false);
  // Mount-time snapshot for the trial-expired check below: render must stay
  // pure (the react-compiler lint rejects Date.now() during render), and the
  // 3-day expiry granularity makes a per-mount timestamp plenty fresh.
  const [nowMs] = useState(() => Date.now());

  const handlePortal = useCallback(async () => {
    capture("billing_portal_opened");
    setPortalLoading(true);
    try {
      await openPortal();
    } catch {
      setPortalLoading(false);
    }
  }, [openPortal]);

  if (isLoading) {
    return (
      <div className="settings-panel" id="panel-billing" role="tabpanel">
        <h1 className="settings-page-title">Billing</h1>
        <p className="settings-page-desc">Manage your subscription.</p>
        <div style={{ display: "flex", justifyContent: "center", padding: "60px 0" }}>
          <div className="settings-spinner" />
        </div>
      </div>
    );
  }

  const subscriptionStatus = account?.subscription_status ?? null;
  // No-card free tier. is_subscribed is FALSE for these rows (paid
  // predicate) even though the platform is usable — the card below renders
  // the free-tier framing with a Subscribe CTA instead of Manage Payment
  // (trial_local rows have no Stripe customer, so the portal would fail).
  const isTrialLocal = subscriptionStatus === "trial_local";
  const trialEndLabel =
    account?.trial_end != null
      ? new Date(account.trial_end * 1000).toLocaleDateString(undefined, {
          month: "short",
          day: "numeric",
          year: "numeric",
        })
      : null;
  // trial_local's `trial_end` is the OVERALL free-tier window expiry — stamped
  // locally at signup as created + 3 days (backend trial_cap._is_expired), NOT
  // the daily $3 reset boundary (which resets at UTC midnight and is tracked
  // separately). Once it elapses the backend blocks usage outright
  // (trial_block_reason -> TRIAL_EXPIRED) WITHOUT flipping subscription_status,
  // so isTrialLocal stays true. Detect the elapsed window here so the card
  // stops promising "$3/day of included usage" and instead shows the paused
  // state — while keeping the Subscribe CTA as the owner's path forward.
  const isTrialExpired =
    isTrialLocal &&
    account?.trial_end != null &&
    account.trial_end * 1000 < nowMs;
  // Display mapping for the status chip — the raw enum must never render
  // ("trial_local" would capitalize to "Trial_local"). Other statuses keep
  // the capitalize treatment. Shared with the two chat shells' plan-badge via
  // @/lib/planLabel so the mapping can't drift between surfaces.
  const statusChipLabel = planLabel(subscriptionStatus, { expired: isTrialExpired });

  // Org context: Teams billing is per-seat, not the flat $50/mo personal
  // plan, so org admins get TeamBillingCard instead of the personal card
  // below — but ONLY when the org is either unsubscribed (the paywall case)
  // or actually on a real per-seat Stripe subscription
  // (account.is_seat_subscription). A subscribed org on a flat/comped/
  // grandfathered sub (no subscription_item_id) has no per-seat Stripe item
  // to price, so showing "N seats × $price" there is fabricated — it falls
  // through to the normal subscribed billing view below instead.
  //
  // Org-level spend is admin-only, full stop — a non-admin member is routed
  // to a standalone "Billing restricted" card before any of the
  // subscription-shape branches below run, so it never sees spend figures
  // or a Manage/Subscribe control regardless of seat-vs-flat shape.
  // TeamBillingCard has its own internal member-vs-admin gate (used by the
  // /chat blocked-screen caller), but we don't reuse it for the member case
  // here because its seat-math line ("N seats × $price") would be fabricated
  // for a flat/comped org.
  const isOrgContext = Boolean(membership);
  const orgRole: "admin" | "member" = membership?.role === "org:admin" ? "admin" : "member";
  const isSeatSubscription = Boolean(account?.is_seat_subscription);

  if (isOrgContext && orgRole === "member") {
    return (
      <div className="settings-panel" id="panel-billing" role="tabpanel">
        <h1 className="settings-page-title">Billing</h1>
        <p className="settings-page-desc">Manage your team&apos;s subscription.</p>
        <div className="settings-card">
          <div className="settings-card-title">Billing restricted</div>
          <div className="settings-card-desc" style={{ marginTop: 4 }}>
            Contact your organization admin to manage billing and subscription settings.
          </div>
        </div>
      </div>
    );
  }

  // Below this point, isOrgContext (when true) implies orgRole === "admin" —
  // the member case returned above.

  if (isOrgContext && (!isSubscribed || isSeatSubscription)) {
    return (
      <div className="settings-panel" id="panel-billing" role="tabpanel">
        <h1 className="settings-page-title">Billing</h1>
        <p className="settings-page-desc">Manage your team&apos;s subscription.</p>

        {accountError && (
          <div className="settings-alert settings-alert-error">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            Failed to load billing data. Please refresh the page.
          </div>
        )}

        <TeamBillingCard
          seatCount={account?.seat_count ?? null}
          seatPriceUsd={account?.team_seat_price_usd ?? null}
          basePriceUsd={account?.team_base_price_usd ?? null}
          ownerRole={orgRole}
          isSubscribed={isSubscribed}
          onSubscribe={startTeamCheckout}
          onManage={handlePortal}
        />
      </div>
    );
  }

  if (isOrgContext) {
    // Subscribed org, NOT a per-seat sub (flat/comped/grandfathered) — the
    // normal subscribed billing view, same shape as the personal card
    // below, restoring the current/lifetime spend line org admins
    // previously saw pre-org-context-rewrite. No per-seat pricing math.
    // orgRole is guaranteed "admin" here (member case returned above).
    return (
      <div className="settings-panel" id="panel-billing" role="tabpanel">
        <h1 className="settings-page-title">Billing</h1>
        <p className="settings-page-desc">Manage your team&apos;s subscription.</p>

        {accountError && (
          <div className="settings-alert settings-alert-error">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            Failed to load billing data. Please refresh the page.
          </div>
        )}

        <div className="settings-card">
          <div className="settings-card-header">
            <div>
              <div className="settings-card-title">
                Lightbulb plan
                {statusChipLabel && (
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4, marginLeft: 8, fontSize: 11, fontWeight: 600, color: "var(--midnight)", background: "color-mix(in srgb, var(--midnight) 8%, transparent)", padding: "2px 8px", borderRadius: 999, textTransform: "capitalize" }}>
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                    {statusChipLabel}
                  </span>
                )}
              </div>
              <div className="settings-card-desc">Active subscription</div>
            </div>
            <button type="button" className="settings-btn-outline" onClick={handlePortal} disabled={portalLoading}>
              {portalLoading && <span className="settings-spinner" style={{ width: 14, height: 14, borderWidth: 2, marginRight: 6, display: "inline-block", verticalAlign: "middle" }} />}
              Manage Payment
            </button>
          </div>
          {/* A flat/comped org sub (no per-seat item) can still upgrade to
              Ultra — only the per-seat TeamBillingCard path omits the CTA,
              and that's a frontend gap, not a backend limit (Ultra now
              supports per-seat subs too). */}
          <UltraUpgradeRow plan={account?.plan} upgradeToUltra={upgradeToUltra} />
        </div>
      </div>
    );
  }

  return (
    <div className="settings-panel" id="panel-billing" role="tabpanel">
      <h1 className="settings-page-title">Billing</h1>
      <p className="settings-page-desc">Manage your subscription.</p>

      {accountError && (
        <div className="settings-alert settings-alert-error">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          Failed to load billing data. Please refresh the page.
        </div>
      )}

      <div className="settings-card">
        <div className="settings-card-header">
          <div>
            <div className="settings-card-title">
              {isTrialLocal ? "Lightbulb — Free tier" : "Lightbulb — $50 / month"}
              {statusChipLabel && (
                <span style={{ display: "inline-flex", alignItems: "center", gap: 4, marginLeft: 8, fontSize: 11, fontWeight: 600, color: "var(--midnight)", background: "color-mix(in srgb, var(--midnight) 8%, transparent)", padding: "2px 8px", borderRadius: 999, textTransform: isTrialLocal ? undefined : "capitalize" }}>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                  {statusChipLabel}
                </span>
              )}
            </div>
            <div className="settings-card-desc">
              {subscriptionStatus === "trialing"
                ? trialEndLabel
                  ? `Free trial — full access until ${trialEndLabel}, then $50/mo. Cancel anytime before then.`
                  : "Free trial — full access now, then $50/mo. Cancel anytime."
                : isTrialLocal
                  ? isTrialExpired
                    ? `Free tier ended${trialEndLabel ? ` on ${trialEndLabel}` : ""} — your agents are paused.`
                    : "Free tier — $3/day of included usage."
                  : isSubscribed
                    ? "Active subscription"
                    : "No active subscription"}
            </div>
          </div>
          {isSubscribed ? (
            <button type="button" className="settings-btn-outline" onClick={handlePortal} disabled={portalLoading}>
              {portalLoading && <span className="settings-spinner" style={{ width: 14, height: 14, borderWidth: 2, marginRight: 6, display: "inline-block", verticalAlign: "middle" }} />}
              Manage Payment
            </button>
          ) : isTrialLocal ? (
            // No CTA in the header — the picker renders full-width below the
            // card. No Manage Payment either: a trial_local row has no Stripe
            // customer, so the portal would fail.
            null
          ) : (
            <Link href="/chat" className="settings-btn-save">
              Sign up
            </Link>
          )}
        </div>
        {/* The SAME picker the chat paywall renders (ChatErrorCard's trial
            branch) — one component, so this surface cannot drift back into
            showing a different set of choices or a different price. It used
            to hand-roll three pill buttons over PROVIDER_SWITCH_CARDS, the
            array documented as the POST-purchase variant that deliberately
            carries no pricing ("the sales framing would be noise" for an
            owner who already subscribed). Rendered pre-purchase, that
            stripped the prices off the one screen whose entire job is to
            show them — including the fact that the Bedrock path costs $70
            today, not $50. */}
        {isTrialLocal && (
          <div style={{ marginTop: 20 }}>
            <ProviderPicker
              embedded
              isOrg={false}
              source="settings_billing"
              heading={isTrialExpired ? "Your agents are paused" : "Subscribe to keep your agents running"}
              subheading="Pick how you want to power your agents. The $50/month covers your always-on infrastructure."
            />
          </div>
        )}
        {/* Ultra upgrade hangs off the paid card for a subscribed owner only —
            an unsubscribed / Sign-up / free-tier owner must pick the base plan
            first. */}
        {isSubscribed && (
          <UltraUpgradeRow plan={account?.plan} upgradeToUltra={upgradeToUltra} />
        )}
      </div>
    </div>
  );
}

// =============================================================================
// Channels Panel
// =============================================================================

function ChannelsPanel() {
  return (
    <div className="settings-panel" id="panel-channels" role="tabpanel">
      <h1 className="settings-page-title">Channels</h1>
      <p className="settings-page-desc">Link your personal Telegram, Discord, and Slack accounts to your organization&apos;s bots.</p>
      <MyChannelsSection />
    </div>
  );
}

// =============================================================================
// Marketplace Panel (seller handle + display name)
// =============================================================================

function MarketplacePanel() {
  return (
    <div className="settings-panel" id="panel-marketplace" role="tabpanel">
      <h1 className="settings-page-title">Marketplace</h1>
      <p className="settings-page-desc">
        Your public identity on the marketplace. The handle appears as
        <span className="font-mono">{" "}@you{" "}</span>
        on every listing byline and on (future) star / credibility surfaces.
      </p>
      <MarketplaceProfileEditor />
    </div>
  );
}

function MarketplaceProfileEditor() {
  const { getToken, isLoaded } = useAuth();
  const [profile, setProfile] = useState<SellerProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    if (!isLoaded) return;
    let cancelled = false;
    (async () => {
      try {
        const jwt = await getToken();
        if (!jwt) return;
        const fetched = await getMySellerProfile({ jwt });
        if (!cancelled) setProfile(fetched);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isLoaded, getToken]);

  if (loading) {
    return <div className="text-ui text-muted-foreground">Loading your profile…</div>;
  }

  return (
    <HandlePicker
      initialProfile={profile}
      saveLabel={profile ? "Update profile" : "Save profile"}
      onSaved={(next) => {
        setProfile(next);
        setSavedAt(Date.now());
      }}
    />
  );
  // savedAt isn't rendered to keep the section minimal — the input border /
  // availability indicator is feedback enough, and the persisted profile is
  // visible in the form fields the moment the request resolves.
  void savedAt;
}

// =============================================================================
// Panel Registry
// =============================================================================

const PANELS: Record<Panel, () => React.ReactElement> = {
  profile: ProfilePanel,
  billing: BillingPanel,
  usage: UsagePanel,
  providers: LLMPanel,
  channels: ChannelsPanel,
  // Settings variant: renders inside the shared `.settings-panel` frame so MCP
  // servers reads as the same surface as the other Settings panels (the Control
  // tab keeps the default full-bleed variant via ControlPanelRouter).
  mcp: () => <IntegrationsPanel variant="settings" />,
  webhooks: WebhooksPanel,
  desktop: DesktopFileAccess,
  // SkillsTab takes an optional `agentId`; mounting it prop-less yields the
  // owner-level (global) skills surface — the engine-room home.
  skills: () => <SkillsTab />,
  "skill-proposals": SkillProposalsPanel,
  "channel-status": ChannelsStatusPanel,
  marketplace: MarketplacePanel,
};

// =============================================================================
// Settings Page
// =============================================================================

export default function SettingsPage() {
  // useSearchParams() forces dynamic rendering; wrap the inner component
  // in a Suspense boundary so the static-export step at build time
  // doesn't error (Next.js 16 requirement for static prerender).
  return (
    <Suspense fallback={null}>
      <SettingsPageInner />
    </Suspense>
  );
}

function SettingsPageInner() {
  // The Settings consolidation: 6-group nav + the config panels relocated from
  // the Control tab (Usage, Model & Providers, MCP servers, the ENGINE ROOM
  // trio, Channel Status).
  const navSections = SETTINGS_NAV_SECTIONS;

  // Personal owners are admins; org members are admins only with org:admin.
  // Reused (not re-derived) from ControlSidebar so the Settings gate matches
  // the Control surface exactly.
  const isAdmin = useIsWorkspaceAdmin();

  // ?panel=<key> preselects the tab. Used by deep links from the chat banners
  // (TrialBanner "Manage" CTA, etc.) so they land on the right pane. Validated
  // against the live PANELS registry — any registered panel is deep-linkable —
  // and admin-only panels fall back to profile for non-admins so a stray
  // ?panel=usage never mounts an admin RPC for a member.
  const searchParams = useSearchParams();
  const initialPanelParam = searchParams.get("panel");
  const initialPanel: Panel =
    initialPanelParam !== null &&
    // hasOwnProperty, not `in` — `in` walks the prototype chain, so
    // ?panel=constructor would otherwise pass validation and read
    // Object.prototype.constructor out of PANELS (panelRegistry.ts's
    // hasOwnKey guards the identical case for the workspace's own registry).
    Object.prototype.hasOwnProperty.call(PANELS, initialPanelParam) &&
    (isAdmin || !ADMIN_ONLY_PANELS.has(initialPanelParam))
      ? (initialPanelParam as Panel)
      : "profile";
  const [activePanel, setActivePanel] = useState<Panel>(initialPanel);
  const ActivePanelComponent = PANELS[activePanel];

  // Arrow-key roving focus operates only over the in-page tabs. Outbound
  // links (e.g. Connections) are excluded so they don't try to set a panel
  // that doesn't exist, and admin-only tabs hidden from non-admins are excluded
  // so roving focus never lands on an unrendered row.
  const tabNavItems = navSections.flatMap((s) => s.items).filter(
    (n): n is NavItem & { panel: Panel } =>
      n.panel !== undefined && (isAdmin || !ADMIN_ONLY_PANELS.has(n.panel)),
  );

  // Accepts the shared NavItem & { panel: string } so it matches SettingsNav's
  // onPanelKeyDown prop; the panel keys are constrained to the Panel union by
  // the section consts, so the cast is sound.
  function handleNavKeyDown(
    e: React.KeyboardEvent,
    item: NavItem & { panel: string },
  ) {
    const idx = tabNavItems.findIndex((n) => n.id === item.id);
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setActivePanel(item.panel as Panel);
    }
    if (e.key === "ArrowDown" || e.key === "ArrowRight") {
      e.preventDefault();
      const next = tabNavItems[(idx + 1) % tabNavItems.length];
      setActivePanel(next.panel);
      document.getElementById(`settings-nav-${next.id}`)?.focus();
    }
    if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
      e.preventDefault();
      const prev = tabNavItems[(idx - 1 + tabNavItems.length) % tabNavItems.length];
      setActivePanel(prev.panel);
      document.getElementById(`settings-nav-${prev.id}`)?.focus();
    }
  }

  return (
    <>

      {/* Top bar */}
      <header className="settings-topbar" style={{ fontFamily: "var(--font-sans)" }}>
        <div className="settings-topbar-left">
          <Logo size="md" href="/chat" />
          <nav className="settings-topbar-breadcrumb" aria-label="Breadcrumb">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <polyline points="9 18 15 12 9 6" />
            </svg>
            <span className="settings-current" aria-current="page">Settings</span>
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

      {/* Main layout */}
      <div className="settings-page-wrap" style={{ fontFamily: "var(--font-sans)" }}>
      <div className="settings-layout">
        <SettingsNav
          mode="tabs"
          sections={navSections}
          isAdmin={isAdmin}
          activePanel={activePanel}
          onSelectPanel={(panel) => setActivePanel(panel as Panel)}
          onPanelKeyDown={handleNavKeyDown}
        />

        <main className="settings-main">
          <ActivePanelComponent key={activePanel} />
        </main>
      </div>
      </div>
    </>
  );
}

