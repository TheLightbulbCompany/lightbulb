"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useClerk, useOrganization } from "@clerk/nextjs";

import { ApiError, useApi } from "@/lib/api";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { Button } from "@/components/ui/button";
import { InvitePeoplePanel } from "@/components/settings/InvitePeoplePanel";
import { Input } from "@/components/ui/input";

type CompanyMember = { userId: string; name?: string | null; email?: string | null; imageUrl?: string | null };

// =============================================================================
// Workspace members — who is here, email invitations, and the link that lets
// someone else in.
//
// Seats are billed automatically from the 6th member
// (billing_service.sync_seat_quantity), so an invitation carries no role:
// everyone joins as a member (roles are Clerk's, below). Email invitations came
// back on 2026-10-08 because people expected an email, not a link hidden here;
// `InvitePeoplePanel` is the same UI the rail's Invite dialog shows, and it owns
// copying the join link. The join-link card below keeps what only an admin on
// this page does with the link: show it, regenerate it, turn it off.
//
// Roles and removal are NOT ours either: "Manage members" opens Clerk's own
// organization screen on its Members tab (owner ruling 2026-10-04). That screen
// also carries Clerk's email invitations and its leave/delete-workspace
// buttons; the Clerk webhooks in routers/webhooks.py already handle all of
// them.
//
// NOTHING below invents a membership model. The roster is `GET /teams/members`,
// the same Clerk-enriched endpoint the chat workspace already renders its human
// roster from (MessagingWorkspaceContext). Redeeming a link creates a Clerk org
// membership, and every downstream effect — the container the new member rides,
// their Paperclip row, their rooms — is the org plumbing that already works.
//
// Styling: `.settings-*` classes (settings.css) for the panel frame and cards,
// shadcn primitives for the controls — the mix the neighbouring settings panels
// use. The stylesheet is imported by the /settings route segment and this
// component is mounted nowhere else, so the classes always resolve.
// =============================================================================

/**
 * `GET /orgs/{org_id}/join-link` — the live link, so it can be re-copied any
 * time. `exists` without `join_url` is a link created before the token was
 * stored encrypted: it works, but only a regenerate yields a copyable one.
 */
export type JoinLinkStatus = {
  exists: boolean;
  join_url?: string | null;
  created_at?: string | null;
  created_by?: string | null;
};

/** `POST /orgs/{org_id}/join-link` — the freshly minted link. */
type CreatedJoinLink = {
  join_url: string;
  created_at: string;
};

/**
 * Turn a backend failure into a sentence.
 *
 * Much shorter than the email-invite version it replaces: these endpoints never
 * forward Clerk's raw error envelope (the redeem path deliberately answers with
 * its own opaque message so it can't be used as an oracle), so `detail` is
 * always either one of our sentences or absent.
 */
export function joinLinkErrorMessage(err: unknown): string {
  const generic = "Something went wrong. Please try again.";
  if (!(err instanceof ApiError)) return generic;

  const detail =
    err.body && typeof err.body === "object" && "detail" in err.body
      ? (err.body as { detail?: unknown }).detail
      : undefined;

  if (err.status === 403) return "Only a workspace admin can manage the join link.";
  if (err.status === 503) return "That service is unavailable right now. Please try again in a moment.";
  return typeof detail === "string" && detail ? detail : generic;
}

/** Display name -> email -> the raw Clerk id, so a row always has a label. */
function labelFor(member: CompanyMember): string {
  return member.name || member.email || member.userId;
}

function initialsOf(member: CompanyMember): string {
  return labelFor(member).trim().charAt(0).toUpperCase() || "?";
}

export function WorkspaceMembersSection({ blurb = "Everyone here shares the same agents, rooms, and subscription." }: { blurb?: string } = {}) {
  // `isLoaded` is load-bearing: Clerk resolves the active org independently of
  // the session, so an unset `organization` during that window is "not known
  // yet", not "personal". Reading it as personal would flash the create-a-
  // workspace card at an org admin (the same race DeleteAccountCard documents).
  const { organization, membership, isLoaded } = useOrganization();
  const { openOrganizationProfile } = useClerk();
  const api = useApi();
  const teams = useTeamsApi();

  const orgId = organization?.id;
  const isAdmin = !organization || membership?.role === "org:admin";

  // Rules of hooks: `read` wraps useSWR, so it is called unconditionally here,
  // above the early returns. A null path is SWR's conditional-fetch signal —
  // no request goes out for a personal owner or a non-admin, both of which
  // render before the roster.
  const showRoster = isLoaded && !!organization && isAdmin;
  const { data: roster, error: rosterError, isLoading: rosterLoading } =
    teams.read<{ members?: CompanyMember[] }>(showRoster ? "/members" : null);

  const [link, setLink] = useState<JoinLinkStatus | null>(null);
  const [linkLoading, setLinkLoading] = useState(true);
  // The link on screen: the backend's current one on load, the fresh one after
  // a mint. Showing it never rotates it — re-sharing used to force a regenerate,
  // which killed the link for everyone who had not clicked yet.
  const [linkUrl, setLinkUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Bumped when the invite panel copies (and so may have minted) the link: re-read it so the
  // card below never offers "Create join link" over a link that now exists.
  const [linkReload, setLinkReload] = useState(0);

  useEffect(() => {
    if (!orgId || !isAdmin) {
      setLinkLoading(false);
      return;
    }
    let cancelled = false;
    setLinkLoading(true);
    api
      .get(`/orgs/${orgId}/join-link`)
      .then((status) => {
        if (cancelled) return;
        const current = status as JoinLinkStatus;
        setLink(current);
        setLinkUrl(current.join_url ?? null);
      })
      .catch((err) => {
        if (!cancelled) setError(joinLinkErrorMessage(err));
      })
      .finally(() => {
        if (!cancelled) setLinkLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [api, orgId, isAdmin, linkReload]);

  const handleGenerate = useCallback(async () => {
    if (!orgId) return;
    setBusy(true);
    setError(null);
    try {
      const created = (await api.post(`/orgs/${orgId}/join-link`, {})) as CreatedJoinLink;
      setLinkUrl(created.join_url);
      setLink({ exists: true, created_at: created.created_at });
    } catch (err) {
      setError(joinLinkErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }, [api, orgId]);

  const handleRevoke = useCallback(async () => {
    if (!orgId) return;
    setBusy(true);
    setError(null);
    try {
      await api.del(`/orgs/${orgId}/join-link`);
      setLinkUrl(null);
      setLink({ exists: false });
    } catch (err) {
      setError(joinLinkErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }, [api, orgId]);

  if (!isLoaded) {
    return (
      <div style={{ display: "flex", justifyContent: "center", padding: "60px 0" }}>
        <div className="settings-spinner" role="status" aria-label="Loading workspace" />
      </div>
    );
  }

  // Personal workspace: there is no org for anyone to join, and the backend
  // would 409 on the missing org claim. Point at the one surface that creates
  // one rather than rendering a control that cannot succeed.
  if (!organization) {
    return (
      <div className="settings-card" data-testid="members-personal">
        <div className="settings-card-title">You&apos;re on a personal workspace</div>
        <div className="settings-card-desc">
          Personal workspaces are just you and your agents — there&apos;s nobody to invite.
          Create a shared workspace to bring teammates in.
        </div>
        <Link href="/onboarding" className="settings-btn-outline" style={{ textDecoration: "none" }}>
          Create a workspace
        </Link>
      </div>
    );
  }

  // Defence in depth. The nav already hides this panel from non-admin members
  // (ADMIN_ONLY_PANELS) and the backend re-checks with `require_org_admin`;
  // this is the third layer, for a member who lands here some other way. It
  // matters more now than it did for the invite form: a join link is a
  // credential, so a member must not be able to mint one.
  if (membership?.role !== "org:admin") {
    return (
      <div className="settings-card" data-testid="members-restricted">
        <div className="settings-card-title">Members restricted</div>
        <div className="settings-card-desc">
          Only a workspace admin can manage who joins {organization.name || "this workspace"}.
        </div>
      </div>
    );
  }

  const members = roster?.members ?? [];

  return (
    <>
      <div className="settings-card" data-testid="members-roster">
        <div className="settings-card-title">In this workspace</div>
        <div className="settings-card-desc">
          {blurb} To make someone an
          admin or remove them, use Manage members.
        </div>

        {rosterLoading && (
          <div style={{ display: "flex", justifyContent: "center", padding: "24px 0" }}>
            <div className="settings-spinner" role="status" aria-label="Loading members" />
          </div>
        )}

        {!rosterLoading && rosterError && (
          <div className="text-ui text-midnight-muted">
            The member list is still coming up. Refresh in a moment.
          </div>
        )}

        {!rosterLoading && !rosterError && members.length === 0 && (
          <div className="text-ui text-midnight-muted">
            Nobody here yet — invite people below.
          </div>
        )}

        {!rosterLoading && !rosterError && members.length > 0 && (
          <ul className="flex flex-col gap-0 list-none p-0 m-0">
            {members.map((member) => (
              <li
                key={member.userId}
                className="flex items-center gap-3 border-b border-edge py-2.5 last:border-b-0"
              >
                {member.imageUrl ? (
                  // eslint-disable-next-line @next/next/no-img-element -- Clerk CDN avatar, no loader config
                  <img
                    src={member.imageUrl}
                    alt=""
                    width={32}
                    height={32}
                    className="h-8 w-8 rounded-full object-cover"
                  />
                ) : (
                  <span
                    aria-hidden="true"
                    className="flex h-8 w-8 items-center justify-center rounded-full bg-cream-deep text-ui font-medium text-midnight"
                  >
                    {initialsOf(member)}
                  </span>
                )}
                <span className="flex flex-col">
                  <span className="text-ui font-medium text-midnight">
                    {labelFor(member)}
                  </span>
                  {/* Only when it isn't already the label — a member with no
                      display name falls back to their email, and printing it
                      twice reads as a rendering bug. */}
                  {member.email && member.email !== labelFor(member) && (
                    <span className="text-sm text-midnight-muted">{member.email}</span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}

        {/* Outside the roster states on purpose: Clerk's screen does not depend
            on our member list having loaded. */}
        <button
          type="button"
          className="settings-btn-outline"
          style={{ marginTop: 12 }}
          onClick={() =>
            openOrganizationProfile({ __experimental_startPath: "/organization-members" })
          }
        >
          Manage members
        </button>
      </div>

      <div className="settings-card" data-testid="members-invite">
        <div className="settings-card-title">Invite people</div>
        <InvitePeoplePanel source="members_settings" onJoinLinkCopied={() => setLinkReload((n) => n + 1)} />
      </div>

      <div className="settings-card" data-testid="members-join-link">
        <div className="settings-card-title">Join link</div>
        <div className="settings-card-desc">
          One link for the whole workspace — send it however you like. Anyone who opens it
          and signs in joins {organization.name || "this workspace"} as a member. Treat it
          like a password: whoever has it gets in.
        </div>

        {linkLoading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "24px 0" }}>
            <div className="settings-spinner" role="status" aria-label="Loading join link" />
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {linkUrl && (
              <div className="flex flex-col gap-1.5">
                <label htmlFor="join-link-url" className="text-ui font-medium text-midnight">
                  Your join link
                </label>
                <Input
                  id="join-link-url"
                  readOnly
                  value={linkUrl}
                  onFocus={(e) => e.currentTarget.select()}
                  className="max-w-[420px] font-mono text-sm"
                />
                <p className="text-sm text-midnight-muted">
                  Send this to anyone you want in — Copy join link above copies it. Regenerating
                  gives you a new link and turns this one off for everyone who hasn&apos;t used it.
                </p>
              </div>
            )}

            {!linkUrl && link?.exists && (
              <p className="text-ui text-midnight-muted" data-testid="join-link-active">
                A join link is active, but it was created before links could be shown again.
                Regenerate once to get one you can copy — the old one stops working
                immediately.
              </p>
            )}

            {!linkUrl && !link?.exists && (
              <p className="text-ui text-midnight-muted" data-testid="join-link-none">
                There&apos;s no join link yet. Create one to let someone in.
              </p>
            )}

            <div className="flex flex-wrap gap-2">
              {linkUrl ? (
                // Secondary once a link is on screen: Copy is the everyday action,
                // Regenerate is the one that locks earlier recipients out.
                <button
                  type="button"
                  className="settings-btn-outline"
                  onClick={handleGenerate}
                  disabled={busy}
                >
                  {busy ? "Working…" : "Regenerate link"}
                </button>
              ) : (
                <Button type="button" onClick={handleGenerate} disabled={busy}>
                  {busy ? "Working…" : link?.exists ? "Regenerate link" : "Create join link"}
                </Button>
              )}
              {link?.exists && (
                <button
                  type="button"
                  className="settings-btn-outline"
                  onClick={handleRevoke}
                  disabled={busy}
                >
                  Turn off link
                </button>
              )}
            </div>
          </div>
        )}

        {error && (
          <div
            className="settings-alert settings-alert-error"
            role="alert"
            style={{ marginTop: 12, marginBottom: 0 }}
          >
            {error}
          </div>
        )}
      </div>
    </>
  );
}
