"use client";

import * as React from "react";
import useSWR from "swr";
import { useAuth, useUser } from "@clerk/nextjs";
import { EditorialDialog } from "@/components/teams/shell/EditorialDialog";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { capture } from "@/lib/analytics";
import { useLaunchInfo } from "@/components/launch/useLaunchInfo";
import { joinErrorCopy } from "@/components/launch/JoinForm";
import { useProvisioningState } from "@/hooks/useProvisioningState";
import type { LaunchMe } from "@/lib/launch";

/**
 * The competition sign-up ask, as a modal over the provisioning screen.
 *
 * This replaces the unjoined half of {@link LaunchJoinBanner}, which is why
 * that component now only renders the JOINED state. A slim strip beside two
 * other banners lost to everything else on the page: with the sheet open and
 * 200 comped months on offer, production had three signups. One deliberate
 * ask beats a permanent nag.
 *
 * WHEN IT FIRES is the whole design, and it is later than "on sign-in":
 *
 *  - `FirstRunFunnel` is the committed onboarding front door and has no skip
 *    (see its module doc). Opening over it would put a competition pitch on
 *    top of the one form that actually starts the owner's workspace. So this
 *    waits for `funnel.state !== "not_started"` — the DURABLE server fact from
 *    `/container/status`, not the `unsafeMetadata` paint cache that
 *    `lib/firstRun` warns against gating on.
 *  - That lands it in the provisioning wait, which is the best moment we have:
 *    the owner has just described their business, the container is still
 *    building, and the screen is otherwise a progress card they can only
 *    watch. Joining no longer needs onboarding to have finished
 *    (`launch_service.join_sheet`), so the ask is answerable right there.
 *
 * Org contexts are excluded outright: `POST /launch/join` refuses every one of
 * them with `org_not_supported` (the sheet keys personal, org billing keys
 * org_id), so showing them a form that can only fail would be a bug. The old
 * banner never checked this.
 *
 * Shown once. Joining or dismissing writes a per-user localStorage key; the
 * name typed here is renameable afterwards via `PATCH /launch/display-name`,
 * which matters because we are now asking minutes after signup, before Friday
 * has researched and named the company.
 */

function seenKey(userId: string): string {
  return `launch_sheet_modal_seen:${userId}`;
}

export function LaunchJoinModal() {
  const api = useApi();
  const { user } = useUser();
  const { orgId } = useAuth();
  const info = useLaunchInfo();
  const { container } = useProvisioningState();

  const open = info?.phase === "open";
  // The brief is in. Anything other than "not_started" means the funnel has
  // been answered, so we are past the one screen we must not cover.
  const briefDone = !!container && container.funnel?.state !== "not_started";
  const eligible = open && !orgId && briefDone;

  const { data: me, mutate } = useSWR<LaunchMe>(
    eligible ? "/launch/me" : null,
    (k: string) => api.get(k) as Promise<LaunchMe>,
  );

  const [seen, setSeen] = React.useState<boolean | null>(null);
  const [name, setName] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [copied, setCopied] = React.useState(false);
  // Set the moment THIS session joins, so the success state stays on screen to
  // be read instead of the dialog vanishing the instant `me.joined` flips.
  const [justJoined, setJustJoined] = React.useState(false);

  React.useEffect(() => {
    if (!user) return;
    try {
      setSeen(window.localStorage.getItem(seenKey(user.id)) === "1");
    } catch {
      setSeen(false); /* storage unavailable — show it, don't swallow the ask */
    }
  }, [user]);

  // Conversion needs a denominator: without a "shown" event the join rate is
  // unmeasurable, and this whole surface exists to be measured against the
  // banner it replaced. Fires once per mount, only when actually visible.
  const shownRef = React.useRef(false);

  const markSeen = React.useCallback(() => {
    setSeen(true);
    if (!user) return;
    try {
      window.localStorage.setItem(seenKey(user.id), "1");
    } catch {
      /* per-session only */
    }
  }, [user]);

  const isOpen = !!(eligible && user && seen === false && me && (!me.joined || justJoined));

  React.useEffect(() => {
    if (!isOpen || shownRef.current) return;
    shownRef.current = true;
    capture("launch_modal_shown", { onboarded: !!me?.onboarded });
  }, [isOpen, me?.onboarded]);

  const join = async () => {
    const displayName = name.trim();
    if (displayName.length < 2 || busy) return;
    setBusy(true);
    setError(null);
    try {
      const joined = (await api.post("/launch/join", {
        display_name: displayName.slice(0, 40),
        public: true,
      })) as LaunchMe;
      // Shared SWR key, so the banner's joined state updates from this too.
      await mutate(joined, { revalidate: false });
      // `source` separates the modal's joins from /launch's JoinForm, which
      // fires the same event.
      capture("launch_joined", { public: true, source: "signup_modal", onboarded: !!me?.onboarded });
      setJustJoined(true);
    } catch (err) {
      setError(joinErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  const copyLink = async () => {
    if (!me?.referral_link) return;
    try {
      await navigator.clipboard.writeText(me.referral_link);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked */
    }
  };

  if (!isOpen || !me || !info) return null;

  if (justJoined) {
    return (
      <EditorialDialog
        open
        onOpenChange={markSeen}
        title={`You're #${me.effective_position ?? me.position} on the sheet`}
        description={`The first ${info.comp_slots.toLocaleString("en-US")} get their first month free. Every referral who subscribes moves you up ${info.referral_jump} spots.`}
        footer={
          <Button type="button" onClick={markSeen}>
            Back to setup
          </Button>
        }
      >
        {me.referral_link ? (
          <div className="space-y-1">
            <p className="text-2xs uppercase tracking-(--tracking-caps) text-[var(--midnight-muted)]">
              Your referral link
            </p>
            <div className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate font-mono text-ui">{me.referral_link}</span>
              <Button type="button" variant="outline" size="sm" onClick={copyLink}>
                {copied ? "Copied" : "Copy"}
              </Button>
            </div>
          </div>
        ) : null}
        <p className="text-ui text-[var(--midnight-muted)]">
          You can change the name shown on the board any time from the launch page.
        </p>
      </EditorialDialog>
    );
  }

  return (
    <EditorialDialog
      open
      onOpenChange={markSeen}
      title="The launch competition is open"
      description={`The first ${info.comp_slots.toLocaleString("en-US")} businesses on the sheet get their first month free. Your workspace is still building — this takes a second.`}
      footer={
        <>
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              capture("launch_modal_dismissed");
              markSeen();
            }}
          >
            Not now
          </Button>
          <Button type="button" onClick={join} disabled={busy || name.trim().length < 2}>
            {busy ? "Joining…" : "Join the sheet"}
          </Button>
        </>
      }
    >
      <label className="block space-y-1" htmlFor="launch-modal-name">
        <span className="text-2xs uppercase tracking-(--tracking-caps) text-[var(--midnight-muted)]">
          Business name
        </span>
        <input
          id="launch-modal-name"
          value={name}
          maxLength={40}
          placeholder="Your company name"
          autoComplete="organization"
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void join();
            }
          }}
          className="w-full rounded border border-[var(--edge)] bg-transparent px-2 py-1.5 text-ui text-[var(--midnight)]"
        />
        <span className="block text-2xs text-[var(--midnight-muted)]">
          Shown on the public board. You can change it later.
        </span>
      </label>
      <p className="text-ui text-[var(--midnight-muted)]">
        Once the competition starts, the business with the biggest revenue lift wins.
      </p>
      {error ? (
        <p role="alert" className="text-ui text-err">
          {error}
        </p>
      ) : null}
    </EditorialDialog>
  );
}
