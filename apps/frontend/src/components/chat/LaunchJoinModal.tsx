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

/** A 409 `not_onboarded` means we are talking to a backend that predates this
 * PR — the gate is gone on current main. See the call site. */
function isNotOnboardedRefusal(err: unknown): boolean {
  const body = (err as { body?: { detail?: { reason?: string } } })?.body;
  return body?.detail?.reason === "not_onboarded";
}

export function LaunchJoinModal() {
  const api = useApi();
  const { user } = useUser();
  const { orgId, isLoaded: authLoaded } = useAuth();
  const info = useLaunchInfo();
  const { container } = useProvisioningState();

  const open = info?.phase === "open";
  // The brief is in. Anything other than "not_started" means the funnel has
  // been answered, so we are past the one screen we must not cover.
  //
  // The `?? "not_started"` is load-bearing, not defensive noise. `funnel` is
  // OPTIONAL on the wire (`routers/container.py` builds it with
  // `contract.get("funnel")`), and `useProvisioningState` hands back the RAW
  // cast of the response body — it never runs through `deriveUIPhase`, whose
  // EMPTY_FUNNEL fallback exists for exactly this. Without the coalesce a
  // missing funnel reads as `undefined !== "not_started"` → true → "brief
  // done", and this dialog opens on top of FirstRunFunnel, which is the one
  // thing its module doc says must never happen. Defaulting a missing state
  // to "not_started" matches every other reader of this field.
  const briefDone = !!container && (container.funnel?.state ?? "not_started") !== "not_started";
  // `|| container.onboarded` catches the legacy cohort ProvisioningStepper
  // documents: owners onboarded through the old `onboarded_at` fact whose
  // `funnel.state` is still "not_started" and for whom the funnel must never
  // re-show. Without this term they fail `briefDone` forever — and since the
  // banner no longer carries a join form, they would have NO in-product way to
  // join at all, despite being exactly the provisioned, established businesses
  // the competition most wants.
  const pastBrief = !!container && (briefDone || !!container.onboarded);
  // `authLoaded` is load-bearing, not belt-and-braces: `useAuth()` reports
  // `orgId: undefined` until Clerk resolves, so without it an org member passes
  // `!orgId` on the first frames and fires a `/launch/me` they can never act
  // on. Org exclusion is meant to be absolute (POST /launch/join refuses every
  // org context), so it has to hold before the session settles too.
  const eligible = open && authLoaded && !orgId && pastBrief;
  // "still building" is a claim about the container, not about the gate above.
  const stillBuilding = !!container && container.runtime_phase !== "ready";

  const { data: me, mutate } = useSWR<LaunchMe>(
    eligible ? "/launch/me" : null,
    (k: string) => api.get(k) as Promise<LaunchMe>,
  );

  const [seen, setSeen] = React.useState<boolean | null>(null);
  // `null` means "untouched, use the prefill" — so the field picks up
  // `suggested_display_name` once `me` resolves (it starts undefined on the
  // first render) WITHOUT an effect, and a real edit still wins. Same shape the
  // banner's join form used before this replaced it.
  //
  // Safe to prefill because the backend only populates the suggestion for an
  // ONBOARDED, non-org, unjoined caller (`launch_service.me`) — i.e. only once
  // Friday has renamed the company to something real. Before that it is null
  // and we fall through to the placeholder, which is exactly the "don't propose
  // a person's name for a business board" rule. Verified live on dev: an
  // onboarded owner's /launch/me returns "Toolchain", and this dialog was
  // making them retype it.
  const [name, setName] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [copied, setCopied] = React.useState(false);
  // Set the moment THIS session joins, so the success state stays on screen to
  // be read instead of the dialog vanishing the instant `me.joined` flips.
  const [justJoined, setJustJoined] = React.useState(false);

  // Keyed on the USER ID, not the user object. Depending on the object makes
  // this re-run on every render that hands back a fresh reference, which turns
  // a localStorage WRITE elsewhere in this component (the deploy-skew path
  // below persists the flag without closing) into an immediate re-read that
  // yanks the dialog off screen mid-error. The id is a stable primitive, so
  // this reads storage once per user, which is the intent.
  const userId = user?.id;
  React.useEffect(() => {
    if (!userId) return;
    try {
      setSeen(window.localStorage.getItem(seenKey(userId)) === "1");
    } catch {
      setSeen(false); /* storage unavailable — show it, don't swallow the ask */
    }
  }, [userId]);

  // Conversion needs a denominator: without a "shown" event the join rate is
  // unmeasurable, and this whole surface exists to be measured against the
  // banner it replaced. Fires once per mount, only when actually visible.
  const shownRef = React.useRef(false);

  // Persist the once-only flag WITHOUT closing the dialog. Split out because
  // the deploy-skew path below needs "don't ask again" while still leaving the
  // error on screen — closing instantly would make the button look like it
  // silently did nothing.
  const persistSeen = React.useCallback(() => {
    if (!userId) return;
    try {
      window.localStorage.setItem(seenKey(userId), "1");
    } catch {
      /* per-session only */
    }
  }, [userId]);

  const markSeen = React.useCallback(() => {
    setSeen(true);
    persistSeen();
  }, [persistSeen]);

  // `me.phase` as well as `info.phase`: `useLaunchInfo` fetches GET /launch
  // ONCE on mount and never revalidates, so a tab open since before the sheet
  // closed still holds "open". `/launch/me` carries its own, fresher phase from
  // the same authenticated read this component already makes — so prefer it and
  // stop offering a join that could only 409.
  const isOpen = !!(
    eligible &&
    user &&
    seen === false &&
    me &&
    me.phase === "open" &&
    (!me.joined || justJoined)
  );

  React.useEffect(() => {
    if (!isOpen || shownRef.current) return;
    shownRef.current = true;
    capture("launch_modal_shown", { onboarded: !!me?.onboarded });
  }, [isOpen, me?.onboarded]);

  const prefill = me?.suggested_display_name ?? "";
  const typed = name ?? prefill;

  const join = async () => {
    const displayName = typed.trim();
    if (displayName.length < 2 || busy) return;
    setBusy(true);
    setError(null);
    try {
      const joined = (await api.post("/launch/join", {
        display_name: displayName.slice(0, 40),
        public: true,
      })) as LaunchMe;
      // BEFORE the mutate, deliberately. `isOpen` requires
      // `(!me.joined || justJoined)`, and `mutate` broadcasts `joined: true`
      // into the shared cache synchronously while the `await` yields — so
      // setting this afterwards leaves a window where React can commit a frame
      // with `me.joined === true` and `justJoined === false`, tearing the
      // dialog down and remounting it (losing focus and the exit animation).
      setJustJoined(true);
      // Shared SWR key, so the banner's joined state updates from this too.
      await mutate(joined, { revalidate: false });
      // `source` separates the modal's joins from /launch's JoinForm, which
      // fires the same event.
      capture("launch_joined", { public: true, source: "signup_modal", onboarded: !!me?.onboarded });
    } catch (err) {
      setError(joinErrorCopy(err));
      // DEPLOY SKEW. Vercel ships this file to prod on merge; the backend that
      // drops the `not_onboarded` refusal only ships on a manual prod dispatch
      // (see CLAUDE.md — "main IS AHEAD OF PROD BY DEFAULT"). In that window
      // the old backend refuses exactly the owners this modal targets. Burn the
      // once-only flag so they are asked ONCE and then left alone, instead of
      // being re-prompted on every page load with copy this PR set out to
      // delete. They can still join from /launch once the backend catches up.
      if (isNotOnboardedRefusal(err)) persistSeen();
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
      // Only claim the workspace is building when it actually is. The gate is
      // "past the brief", not "currently provisioning", so the largest audience
      // for this dialog is every existing owner who never joined — telling them
      // their long-running container is still building would be plainly false.
      description={
        stillBuilding
          ? `The first ${info.comp_slots.toLocaleString("en-US")} businesses on the sheet get their first month free. Your workspace is still building — this takes a second.`
          : `The first ${info.comp_slots.toLocaleString("en-US")} businesses on the sheet get their first month free. This takes a second.`
      }
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
          <Button type="button" onClick={join} disabled={busy || typed.trim().length < 2}>
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
          value={typed}
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
