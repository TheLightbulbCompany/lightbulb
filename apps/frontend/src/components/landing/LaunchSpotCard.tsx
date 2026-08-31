"use client";

import * as React from "react";
import { ApiError, useApi } from "@/lib/api";
import type { LaunchInfo, LaunchMe } from "@/lib/launch";
import { LaunchStatusLines } from "@/components/launch/LaunchStatusLines";
import { useReferralAttribution } from "@/components/launch/useReferralAttribution";
import { LANDING_BRIEF_STORAGE_KEY, useLandingBriefReplay } from "@/hooks/useLandingBriefReplay";
import "@/components/launch/launch.css";

/**
 * The held owner's waitlist spot, rendered in the landing hero slot by
 * HeroFunnel once `/launch/me` confirms the pre-drop hold. During the
 * waitlist phase the landing page IS the whole waitlist experience, so this
 * card is self-sufficient: it replays a stashed landing brief (the shared
 * useLandingBriefReplay implementation — same one /chat's ProvisioningStepper
 * uses), attributes a stored referral code, and lets the owner drop or edit
 * the idea Friday will run the second doors open.
 */

const ET = "America/New_York";

/** Client-side mirror of the backend's `_doors_open_copy` (launch_hold.py) —
 * same ET wall-time copy the in-chat held card shows via `blocked.message`.
 * `\u202f` (ICU's narrow no-break space before AM/PM) is normalized so the
 * string matches the backend's byte-for-byte. */
function doorsOpenCopy(dropAt: string): string {
  const d = new Date(dropAt);
  const day = new Intl.DateTimeFormat("en-US", { timeZone: ET, weekday: "long", month: "long", day: "numeric" }).format(d);
  const time = new Intl.DateTimeFormat("en-US", { timeZone: ET, hour: "numeric", minute: "2-digit", hour12: true })
    .format(d)
    .replace(/\u202f/g, " ");
  return `Doors open ${day} at ${time} ET. Sign back in then and Friday runs your idea.`;
}

/** Display-only peek at the stashed landing brief, taken at render time —
 * the replay hook consumes (deletes) the stash in a mount effect, which runs
 * after this. Lets the card show the just-replayed idea without waiting for a
 * /launch/me refetch. */
function peekStashedIdea(): string | null {
  try {
    const raw = window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    const input =
      parsed !== null && typeof parsed === "object" && typeof (parsed as { input?: unknown }).input === "string"
        ? ((parsed as { input: string }).input).trim()
        : "";
    return input.length >= 3 && input.length <= 2000 ? input : null;
  } catch {
    return null;
  }
}

/** The brief route's 422s come in two shapes: a plain-string detail (an
 * HTTPException) and Pydantic's model_validator list
 * (`detail: [{msg: "Value error, ..."}]`) — the min-3-chars rule is the
 * latter, and the shared client's detail extraction leaves it as "API 422".
 * Surface the first item's msg, minus Pydantic's boilerplate prefix. */
function saveErrorCopy(err: unknown): string {
  if (err instanceof ApiError && err.body && typeof err.body === "object") {
    const detail = (err.body as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const msg = (detail[0] as { msg?: unknown } | undefined)?.msg;
      if (typeof msg === "string") return msg.replace(/^Value error, /, "");
    }
  }
  return "Couldn't save that — try again.";
}

export function LaunchSpotCard({ me, info }: { me: LaunchMe; info: LaunchInfo }) {
  const api = useApi();
  useReferralAttribution(api, me);
  const [stashedIdea] = React.useState<string | null>(peekStashedIdea);
  // The card only mounts when the owner is held, so the gate arguments are
  // the held truth: same firing state the /chat stepper reports.
  const { replaying } = useLandingBriefReplay({
    gatePhase: "blocked",
    blockedCode: "launch_pending",
    awaitingBrief: true,
    isTerminalError: false,
  });

  const [idea, setIdea] = React.useState<string | null>(me.idea_full ?? null);
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  // A dispatched replay upserted the stash over any stored brief — show it.
  React.useEffect(() => {
    if (replaying && stashedIdea) setIdea(stashedIdea);
  }, [replaying, stashedIdea]);

  // `replaying` settles true→false only when the replay POST did NOT land
  // (the hook restored the stash). The optimistic peek above may now be
  // showing a phantom "Your idea" — reconcile from the server: idea_full
  // present → the real saved view; absent → back to the empty prompt, and
  // the restored stash gets its normal peek/replay cycle on the next visit.
  // The saved-idea view is only ever backed by a server response or a
  // successful save, never by the peek alone once replay has settled.
  const prevReplaying = React.useRef(false);
  React.useEffect(() => {
    const settled = prevReplaying.current && !replaying;
    prevReplaying.current = replaying;
    if (!settled) return;
    let alive = true;
    api
      .get("/launch/me")
      .then((raw) => {
        if (alive) setIdea((raw as LaunchMe).idea_full ?? null);
      })
      .catch(() => {
        /* unreachable server — keep the current view; next visit reconciles */
      });
    return () => {
      alive = false;
    };
  }, [replaying, api]);

  // Mirror of the backend BriefBody rules: blank is the "you pick" flow,
  // non-blank must be >= 3 chars after strip (422 otherwise).
  const trimmed = draft.trim();
  const canSave = trimmed.length === 0 || trimmed.length >= 3;

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSave || saving) return;
    setSaving(true);
    setError(null);
    try {
      // Same body shape the stash replay sends: always path "new", empty
      // input absent rather than "".
      const resp = await api.post("/onboarding/brief", { path: "new", input: trimmed || undefined });
      // "staged" is the one success status. "already_submitted" is a refused
      // overwrite (the backend couldn't confirm the held state) and must not
      // render as saved.
      if ((resp as { status?: string } | null)?.status !== "staged") {
        setError("Couldn't save that — your earlier idea is still the one on file. Try again.");
        return;
      }
      setIdea(trimmed || null);
      setEditing(false);
      setDraft("");
    } catch (err) {
      setError(saveErrorCopy(err));
    } finally {
      setSaving(false);
    }
  };

  const showForm = editing || idea === null;

  return (
    <div className="lc-card lc-card--spot" data-testid="launch-spot-card">
      <LaunchStatusLines me={me} dropAt={info.drop_at} showIdea={false} />
      {showForm ? (
        <form className="lc-field" onSubmit={save}>
          <span>{idea === null ? "Drop your idea now — Friday starts on it the second doors open." : "Your idea"}</span>
          <textarea
            maxLength={2000}
            rows={3}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-label="Your idea"
            placeholder="A ceramics studio that ships nationwide…"
          />
          <div className="lc-actions">
            <button type="submit" className="ld-btn" disabled={!canSave || saving}>
              Save idea
            </button>
            {idea !== null ? (
              <button
                type="button"
                className="ld-btn ld-btn--ghost"
                onClick={() => {
                  setEditing(false);
                  setError(null);
                }}
              >
                Cancel
              </button>
            ) : null}
          </div>
          {error ? (
            <p role="alert" className="lc-alert">
              {error}
            </p>
          ) : null}
        </form>
      ) : (
        <div className="lc-field">
          <span>Your idea</span>
          <p className="lc-idea">{idea}</p>
          <button
            type="button"
            className="ld-btn ld-btn--ghost"
            onClick={() => {
              setDraft(idea ?? "");
              setError(null);
              setEditing(true);
            }}
          >
            Edit
          </button>
        </div>
      )}
      <p className="lc-fine" style={{ marginTop: 16 }}>
        {doorsOpenCopy(info.drop_at)}
      </p>
    </div>
  );
}
