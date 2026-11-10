/**
 * The single PostHog touchpoint for the entire frontend.
 *
 * Every call into the PostHog SDK (init, identify, reset, capture,
 * pageview, captureException) lives behind a function in this file, and
 * the React tree's ``<Provider>`` is re-exported from here too. The rule
 * is: ``import "posthog-js"`` and ``import "posthog-js/react"`` only ever
 * appear in this module — every other file in ``apps/frontend/src/``
 * imports from ``@/lib/analytics``.
 *
 * Why: the previous shape had ``usePostHog()`` direct in 14 components,
 * which meant "switch analytics provider" or "add a global property to
 * every event" or "stop sending events from a specific page" all
 * required touching every component. The wrapper existed but was
 * bypassed — the seam was hypothetical, not real (per Matt Pocock's
 * ``improve-codebase-architecture`` skill: one adapter is a hypothetical
 * seam, two is a real one). Routing every caller through this module
 * makes the seam load-bearing and gives PostHog one mockable surface
 * for tests.
 *
 * What belongs here:
 *   - User intents (chat_message_sent, subscription_checkout_started)
 *   - User-visible outcomes (chat_completed, channel_link_submitted)
 *   - The React Provider wrapper (``Provider``) for the app's root layout
 *   - identify / reset / pageview / init bookkeeping for PostHogProvider
 *
 * What does NOT belong here:
 *   - RPC-call-level events (backend HTTP activity goes to CloudWatch —
 *     don't shadow it in PostHog)
 *   - PII beyond identify-level fields (specifically: no message content,
 *     no bot tokens, no API key values — capture lengths / types only)
 */

import posthog from "posthog-js";
import { PostHogProvider as PHProvider } from "posthog-js/react";

/**
 * The single PostHog client instance. Re-exported so
 * ``components/PostHogProvider.tsx`` can hand it to ``<Provider>``
 * without importing ``posthog-js`` itself.
 */
export const client = posthog;

/**
 * The PostHog React provider, re-exported so the app's root layout
 * doesn't need to ``import "posthog-js/react"`` directly.
 */
export const Provider = PHProvider;

/**
 * The registry of every product event we emit. Adding a new event?
 * Add it here first — TypeScript will flag every call site that needs
 * to be updated.
 *
 * Strict union (rather than ``string``) is the load-bearing piece: it
 * catches typos at compile time and makes "what events do we emit?" a
 * grep-able question with one answer.
 *
 * Grouped roughly by surface for readability; keep alphabetical within
 * each group when adding new entries so diffs stay reviewable.
 */
export type AnalyticsEvent =
  // Super-agent threads (wave 2). Properties: taskId, source, status, count only; never titles or text.
  | "overview_opened"
  | "task_card_opened"
  | "task_question_answered"
  | "task_reopened"
  | "task_resolved"
  | "task_retried"
  | "task_steer_sent"
  | "task_stopped"
  // Growth: the door into a workspace and the sharing surface (2026-09-30)
  | "join_link_copied"
  | "session_opened"
  | "share_link_full_viewed"
  | "join_link_viewed"
  | "join_command_copied"
  | "share_link_cta_clicked"
  // Agent lifecycle
  | "agent_create_chooser"
  | "agent_created"
  | "agent_deleted"
  | "agent_model_changed"
  | "agent_renamed"
  | "agent_selected"
  // Billing
  | "billing_portal_opened"
  | "billing_plan_chosen"
  // Settings → Profile: an owner saved the workspace ZIP export.
  | "workspace_export_downloaded"
  // Paired with `first_run_failed_notice_shown` {cause: "credits"}: without a
  // click event, the question the credits branch was built to answer — do
  // out-of-credits owners self-serve instead of emailing us — cannot be
  // answered from the data at all.
  | "credits_topup_clicked"
  | "subscription_checkout_canceled"
  | "subscription_checkout_failed"
  | "subscription_checkout_returned_success"
  | "subscription_checkout_started"
  // Catalog
  | "catalog_agent_deployed"
  | "catalog_agent_published"
  // Channels
  | "channel_connected"
  | "channel_link_submitted"
  // Chat surface
  | "chat_aborted"
  | "chat_completed"
  | "chat_file_uploaded"
  | "chat_message_sent"
  | "chat_stopped"
  | "session_deleted"
  // Container / control panel
  | "control_panel_opened"
  | "file_browser_opened"
  | "browser_view_opened"
  | "ideas_opened"
  | "teams_degraded_notice_shown"
  | "first_run_failed_notice_shown"
  | "update_applied"
  | "update_scheduled"
  // Desktop app
  | "desktop_update_check_clicked"
  | "desktop_update_install_clicked"
  | "desktop_file_access_added"
  | "desktop_file_access_removed"
  | "desktop_permissions_saved"
  // MCP servers running on the user's own Mac (Tauri local_mcp_* commands)
  | "local_mcp_server_removed"
  | "local_mcp_servers_saved"
  // Cron
  | "cron_job_created"
  | "cron_job_deleted"
  | "cron_job_toggled"
  | "cron_job_triggered"
  // First-run funnel
  | "first_run_brief_submitted"
  // Landing
  | "install_line_copied"
  | "landing_download_clicked"
  | "landing_viewed"
  | "setup_cta_clicked"
  // Integrations
  | "integration_access_requested"
  // MCP
  | "mcp_server_added"
  | "mcp_server_removed"
  // Messaging — room_kind + workspace_id only; a room's name can be customer content
  | "room_opened"
  | "composer_typing_started"
  // Notifications
  | "notification_clicked"
  | "notification_opened"
  | "notification_shown"
  | "run_notification_clicked"
  | "run_notification_shown"
  // Plugins
  | "plugin_toggled"
  // Onboarding
  | "onboarding_gate_blocked"
  | "onboarding_provider_completed"
  | "onboarding_ready"
  | "onboarding_step_completed"
  | "onboarding_step_entered"
  | "org_invitation_accepted"
  | "trial_started"
  | "workspace_type_selected"
  // Signup
  | "signup_cta_clicked"
  | "signup_exit"
  | "signup_started"
  | "source_reported"
  // Skills
  | "skill_api_key_saved"
  | "skill_installed"
  | "skill_toggled"
  | "clawhub_searched"
  // Support
  | "support_ticket_submitted"
  | "support_contact_clicked"
  // Workspace shell
  | "workspace_layout_reset"
  | "workspace_panel_opened"

  // Launch competition
  | "launch_joined"
  | "launch_modal_shown"
  | "launch_modal_dismissed"
  | "launch_renamed"
  | "launch_stripe_connected"
  | "launch_referral_copied"
  | "launch_team_interest"
  // Cards in chat
  | "openui.parse_error";

/**
 * Type exposing the ``__loaded`` flag PostHog sets after ``init()``
 * resolves. Declared locally so we don't have to import a private type
 * — the field is part of the public contract (documented at
 * https://posthog.com/docs/libraries/js).
 */
interface PostHogLoadState {
  __loaded?: boolean;
}

function isLoaded(): boolean {
  return (posthog as unknown as PostHogLoadState).__loaded === true;
}

const ATTRIBUTION_STORAGE_KEY = "isol8_attribution_v1";
const UTM_KEYS = [
  "utm_source",
  "utm_medium",
  "utm_campaign",
  "utm_term",
  "utm_content",
  "gclid",
  "fbclid",
  "msclkid",
  "ttclid",
  "li_fat_id",
] as const;

type AttributionValue = string | null;
type AttributionPayload = Record<string, AttributionValue>;

interface StoredAttribution {
  first_touch: AttributionPayload;
  last_touch: AttributionPayload;
}

function safeNow(): string {
  return new Date().toISOString();
}

function emptyTouch(timestamp: string): AttributionPayload {
  return {
    timestamp,
    landing_path: null,
    referrer: null,
    referring_domain: null,
    utm_source: null,
    utm_medium: null,
    utm_campaign: null,
    utm_term: null,
    utm_content: null,
    gclid: null,
    fbclid: null,
    msclkid: null,
    ttclid: null,
    li_fat_id: null,
  };
}

function readStoredAttribution(): StoredAttribution | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(ATTRIBUTION_STORAGE_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as StoredAttribution;
  } catch {
    return null;
  }
}

function writeStoredAttribution(value: StoredAttribution): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(ATTRIBUTION_STORAGE_KEY, JSON.stringify(value));
  } catch {
    // Attribution is helpful, not product-critical. Storage-disabled browsers
    // should still get a working app and best-effort current-session events.
  }
}

function externalReferrer(): { referrer: string | null; domain: string | null } {
  if (typeof document === "undefined" || !document.referrer) {
    return { referrer: null, domain: null };
  }
  try {
    const referrer = new URL(document.referrer);
    if (typeof window !== "undefined" && referrer.host === window.location.host) {
      return { referrer: null, domain: null };
    }
    return { referrer: document.referrer, domain: referrer.hostname };
  } catch {
    return { referrer: document.referrer, domain: null };
  }
}

// The product app, not the site. Every other path (/, /pricing, /request-setup, /docs, /blog,
// comparison and legal pages, the [seoSlug] pages) is a marketing route for landing_viewed.
const APP_ROUTE_PREFIXES = [
  "/chat", "/s", "/settings", "/onboarding", "/sign-in", "/sign-up", "/admin", "/join", "/welcome",
  "/auth", "/oauth", "/checkout_redirect", "/session-tasks", "/marketplace", "/meet-bot", "/api",
];

/** A page of the website (T23): where a visit lands, as opposed to the signed-in app. */
export function isMarketingRoute(pathname: string | null | undefined): boolean {
  if (!pathname) return false;
  return !APP_ROUTE_PREFIXES.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}

/** Routes whose DOM is a customer's conversation (/chat, /s) or names people and carries an
 *  invitation credential (/join/*): never recorded, never autocaptured. */
export function isPrivateRoute(pathname: string | null | undefined): boolean {
  return pathname === "/chat" || pathname === "/s" || pathname === "/join" || !!pathname?.startsWith("/join/");
}

// /chat deep links can carry file paths in `?path=`, and /s carries a session
// link's token in its fragment (a read credential) and the CLI's loopback port.
// /join/{org}#token carries a join-link credential in its fragment, and
// /join/invite carries Clerk's invitation ticket plus the inviter's and the
// workspace's names. The route is enough for analytics; keep the query string
// and fragment out of pageviews, replay URLs and SDK defaults.
const JOIN_ROUTE = /^\/join(\/|$)/;
// Clerk forwards its invitation ticket to /sign-up and /sign-in (and the invite
// page passes it on): drop those params anywhere, keep the rest (UTM).
// redirect_url: the invite page points it at the approval card the invitation carries
// (/chat?panel=teams-approval-detail&detailId=…), which is the workspace's private id.
const SECRET_PARAMS = ["__clerk_ticket", "__clerk_status", "inviter", "workspace", "decision", "approval", "redirect_url"];

export function withoutChatQuery(value: string): string {
  try {
    const url = new URL(value, "https://thelightbulb.company");
    // /j carries a join ticket in its fragment: keep the route and query, drop the fragment.
    if (url.pathname === "/j") return value.split("#", 1)[0];
    if (isPrivateRoute(url.pathname) || JOIN_ROUTE.test(url.pathname)) return value.split(/[?#]/, 1)[0];
    if (!SECRET_PARAMS.some((key) => url.searchParams.has(key))) return value;
    for (const key of SECRET_PARAMS) url.searchParams.delete(key);
    const absolute = /^[a-z]+:\/\//i.test(value);
    const rest = `${url.pathname}${url.search}${url.hash}`;
    return absolute ? `${url.origin}${rest}` : rest;
  } catch {
    return value;
  }
}

function currentTouch(): AttributionPayload {
  const timestamp = safeNow();
  const touch = emptyTouch(timestamp);
  if (typeof window === "undefined") return touch;

  const params = new URLSearchParams(window.location.search);
  for (const key of UTM_KEYS) {
    touch[key] = params.get(key);
  }
  const ref = externalReferrer();
  touch.referrer = ref.referrer;
  touch.referring_domain = ref.domain;
  touch.landing_path = withoutChatQuery(`${window.location.pathname}${window.location.search}`);
  return touch;
}

function hasAttributionSignal(touch: AttributionPayload): boolean {
  return Boolean(
    touch.referrer ||
      UTM_KEYS.some((key) => {
        const value = touch[key];
        return typeof value === "string" && value.length > 0;
      }),
  );
}

export function refreshAttribution(): StoredAttribution | null {
  if (typeof window === "undefined") return null;
  const previous = readStoredAttribution();
  const touch = currentTouch();

  if (!previous) {
    const initial = { first_touch: touch, last_touch: touch };
    writeStoredAttribution(initial);
    return initial;
  }

  if (!hasAttributionSignal(touch)) return previous;

  const next = { ...previous, last_touch: touch };
  writeStoredAttribution(next);
  return next;
}

const WORKSPACE_DOOR_KEY = "lightbulb:workspace_door";

/** The door a person used on the way to "Create your workspace" (T16's session page marks
 *  `session_link`); the workspace form sends it with the source (T12). Tab-scoped
 *  (sessionStorage): an abandoned visit must not label a later, unrelated signup. */
export function markWorkspaceDoor(door: "session_link"): void {
  try {
    window.sessionStorage.setItem(WORKSPACE_DOOR_KEY, door);
  } catch {
    // analytics only
  }
}

/** Forget the door once a workspace was created from it: it describes this visit, not history. */
export function clearWorkspaceDoor(): void {
  try {
    window.sessionStorage.removeItem(WORKSPACE_DOOR_KEY);
  } catch {
    // analytics only
  }
}

export function readWorkspaceDoor(): string | null {
  try {
    return window.sessionStorage.getItem(WORKSPACE_DOOR_KEY);
  } catch {
    return null;
  }
}

/** The stored first- and last-touch records, for a form that hands them to the backend
 *  (the workspace form, T12; the setup form, T22). Empty objects when nothing is stored. */
export function touchPayload(): { first_touch: AttributionPayload; last_touch: AttributionPayload } {
  const stored = readStoredAttribution();
  return { first_touch: stored?.first_touch ?? {}, last_touch: stored?.last_touch ?? {} };
}

export function attributionProperties(): Record<string, unknown> {
  const stored = readStoredAttribution();
  if (!stored) return {};

  const props: Record<string, unknown> = {};
  for (const [prefix, touch] of [
    ["first_touch", stored.first_touch],
    ["last_touch", stored.last_touch],
  ] as const) {
    for (const [key, value] of Object.entries(touch)) {
      if (value != null && value !== "") {
        props[`${prefix}_${key}`] = value;
      }
    }
  }
  return props;
}

/**
 * Bootstrap PostHog. Idempotent at the SDK level (PostHog's own
 * ``init`` is a no-op on second call), but we also short-circuit on
 * SSR so a server render doesn't try to touch ``window``.
 *
 * Called from ``components/PostHogProvider.tsx`` exactly once at module
 * eval. Every other code path calls ``capture`` / ``identify`` /
 * ``reset`` / ``pageview`` / ``captureException`` and lets those guards
 * handle the "PostHog never initialised" case (e.g. when
 * ``NEXT_PUBLIC_POSTHOG_KEY`` is unset locally).
 */
export function init(
  apiKey: string,
  options: Parameters<typeof posthog.init>[1],
): void {
  if (typeof window === "undefined") return;
  posthog.init(apiKey, options);
}

/**
 * Capture a product event. Safe to call from hooks/components that may
 * render during SSR (returns early without touching PostHog) and from
 * environments where ``NEXT_PUBLIC_POSTHOG_KEY`` is unset (returns
 * early because ``init()`` was never called and ``__loaded`` is false).
 */
export function capture(
  event: AnalyticsEvent,
  properties?: Record<string, unknown>,
  options?: Parameters<typeof posthog.capture>[2],
): void {
  if (typeof window === "undefined") return;
  if (!isLoaded()) return;
  posthog.capture(event, { ...attributionProperties(), ...properties }, options);
}

/**
 * Register super properties — merged into EVERY subsequent event by
 * PostHog itself. Used for ``signup_flow_id`` and the browser-context
 * pair, which have to ride events emitted from call sites that have no
 * business knowing about either.
 *
 * Note ``reset()`` clears super properties, so anything registered here
 * must be re-registered after a sign-out (see PostHogProvider).
 */
export function register(properties: Record<string, unknown>): void {
  if (typeof window === "undefined") return;
  if (!isLoaded()) return;
  posthog.register(properties);
}

/**
 * Forward an unhandled browser error to PostHog's error tracking. Used
 * by the global error/unhandledrejection forwarders installed in
 * ``components/PostHogProvider.tsx``.
 */
export function captureException(error: unknown): void {
  if (typeof window === "undefined") return;
  if (!isLoaded()) return;
  // posthog-js >= 1.190 exposes captureException directly.
  posthog.captureException(error);
}

/**
 * Bind a stable distinctId (currently the Clerk user id) plus an
 * identify-payload of profile fields. Called from
 * ``components/PostHogProvider.tsx`` on the rising edge of
 * ``isSignedIn`` so events captured before identify are merged onto
 * the right person.
 */
export function identify(
  distinctId: string,
  properties?: Record<string, unknown>,
): void {
  if (typeof window === "undefined") return;
  if (!isLoaded()) return;
  posthog.identify(distinctId, { ...attributionProperties(), ...properties });
}

/**
 * Clear the bound distinctId and reset the local PostHog state. Called
 * from ``components/PostHogProvider.tsx`` on sign-out.
 */
export function reset(): void {
  if (typeof window === "undefined") return;
  if (!isLoaded()) return;
  posthog.reset();
}

/**
 * Capture a manual pageview event. ``capture_pageview`` is disabled in
 * the ``init()`` options so the App-Router page tracking goes through
 * here instead — Next.js ``usePathname()``/``useSearchParams()`` give
 * us the URL without relying on PostHog's broken default
 * pushState-detection in App Router.
 */
export function pageview(url: string): boolean {
  if (typeof window === "undefined") return false;
  refreshAttribution();
  if (!isLoaded()) return false;
  posthog.capture("$pageview", { ...attributionProperties(), $current_url: withoutChatQuery(url) });
  return true;
}
