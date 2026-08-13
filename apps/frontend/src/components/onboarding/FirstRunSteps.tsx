"use client";

/**
 * Presentational step UI for the first-run funnel: the two-path picker and
 * the idea/website input screens. Deliberately Clerk-free — all state and
 * submission live in the caller. FirstRunFunnel wraps it for the in-chat
 * onboarding front door; the landing hero renders it pre-auth via the
 * `defaultPath` variant.
 */
import * as React from "react";

export type Path = "new" | "grow";
/** "noIdea" = they clicked "I don't know yet — you pick": submit with no input. */
export type Mode = "idea" | "noIdea";

// After this many CONSECUTIVE failed submits carrying the same upstream
// error, the generic "Couldn't save that just yet. Retry" line — which
// implies a transient problem — gives way to a persistent error state that
// shows the upstream detail and points at support. A different error (or a
// success) resets the streak.
const PERSISTENT_FAILURE_THRESHOLD = 3;

// Brand-primary recipe used across the app (see ProviderPicker / landing
// .ld-btn): forest fill (#06402B), cream text, pill, hover #0a5138.
const CHOICE_BTN =
  "w-full rounded-full bg-[var(--forest)] hover:bg-[#0a5138] text-[var(--cream)] py-4 text-sm font-semibold tracking-wide transition-colors";
const INPUT =
  "w-full rounded-xl border border-[var(--edge)] bg-white p-3 text-sm text-[var(--ink)] placeholder:text-[var(--mute)] focus:outline-none focus:border-[var(--forest)]";
// Outline, not filled: this is the escape hatch for the undecided, and it
// should not compete with Continue for the user who already has an idea.
const SECONDARY_BTN =
  "w-full rounded-full border border-[#d6d0c3] bg-transparent hover:bg-[#f1ede4] text-[var(--ink)] py-3 text-sm font-medium tracking-wide transition-colors disabled:opacity-50";

const QUIET_LINK =
  "text-xs text-[var(--mute)] hover:text-[var(--ink)] transition-colors";

export interface FirstRunStepsProps {
  path: Path | null;
  input: string;
  detail: string;
  busy: boolean;
  /** Which submission is in flight / retryable — keeps the busy copy on the right button. */
  mode: Mode;
  /** Non-null renders the failure copy; `streak` at the threshold switches to the persistent support state. */
  error: { detail: string; streak: number } | null;
  onSetPath: (path: Path | null) => void;
  onInput: (value: string) => void;
  onDetail: (value: string) => void;
  onSubmit: (mode: Mode) => void;
  /**
   * Landing-hero layout variant: skip the two-path picker and open straight
   * on the idea input, with a quiet grow/new toggle in place of the back
   * button. Omitted (the in-chat funnel), the classic two-path first screen
   * renders and Back returns to it.
   */
  defaultPath?: "new";
}

export function FirstRunSteps({
  path,
  input,
  detail,
  busy,
  mode,
  error,
  onSetPath,
  onInput,
  onDetail,
  onSubmit,
  defaultPath,
}: FirstRunStepsProps) {
  const activePath = path ?? defaultPath ?? null;

  if (activePath === null) {
    return (
      <div className="space-y-8 text-center">
        <div className="space-y-2">
          <h1 className="text-3xl font-semibold tracking-tight text-[var(--ink)] font-lora">
            Let&apos;s get started.
          </h1>
          <p className="text-sm text-[var(--mute)]">
            Tell your CEO about your business — she&apos;ll research it and lay out your next moves.
          </p>
        </div>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <button className={CHOICE_BTN} onClick={() => onSetPath("new")}>
              Start something new
            </button>
            <p className="text-xs italic text-[var(--mute)]">Start from scratch</p>
          </div>
          <div className="space-y-1.5">
            <button className={CHOICE_BTN} onClick={() => onSetPath("grow")}>
              Grow my business
            </button>
            <p className="text-xs italic text-[var(--mute)]">Already have a business</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {!defaultPath && (
        <button className={QUIET_LINK} onClick={() => onSetPath(null)}>
          ← Back
        </button>
      )}
      <h1 className="text-2xl font-semibold tracking-tight text-[var(--ink)] font-lora">
        {activePath === "new" ? "What's your idea?" : "What's your company's website?"}
      </h1>
      {activePath === "new" ? (
        <div className="space-y-3">
          <textarea
            className={`${INPUT} min-h-28`}
            placeholder="e.g. A social media agency for small restaurants"
            value={input}
            onChange={(e) => onInput(e.target.value)}
          />
          <input
            className={INPUT}
            placeholder="Who's it for? (optional)"
            value={detail}
            onChange={(e) => onDetail(e.target.value)}
          />
        </div>
      ) : (
        <input
          className={INPUT}
          placeholder="yourcompany.com"
          value={input}
          onChange={(e) => onInput(e.target.value)}
        />
      )}
      {defaultPath && (
        <button
          className={QUIET_LINK}
          onClick={() => onSetPath(activePath === "new" ? "grow" : "new")}
        >
          {activePath === "new"
            ? "Already have a business? Grow it instead"
            : "Starting from scratch? Describe your idea instead"}
        </button>
      )}
      <div className="space-y-3">
        <button
          className={`${CHOICE_BTN} disabled:opacity-50`}
          disabled={busy || input.trim().length < 3}
          onClick={() => onSubmit("idea")}
        >
          {busy && mode === "idea" ? "Setting things up…" : "Continue →"}
        </button>
        {activePath === "new" && (
          <div className="space-y-1.5 pt-1">
            <button
              className={SECONDARY_BTN}
              disabled={busy}
              onClick={() => onSubmit("noIdea")}
            >
              {busy && mode === "noIdea"
                ? "Setting things up…"
                : "I don’t know yet — you pick"}
            </button>
            <p className="text-xs italic text-center text-[var(--mute)]">
              Friday will look you up and choose something
            </p>
          </div>
        )}
      </div>
      {error &&
        (error.streak >= PERSISTENT_FAILURE_THRESHOLD ? (
          // Persistent failure: the same upstream error N times in a
          // row is not transient — stop implying a retry will fix it.
          // Show the upstream detail and route to support instead.
          <div className="space-y-1.5 text-sm text-[var(--danger)]">
            <p>We couldn&apos;t save your brief.</p>
            <p className="break-words text-xs text-[var(--mute)]">{error.detail}</p>
            <p>
              Please{" "}
              <a className="underline" href="mailto:support@isol8.co">
                contact support
              </a>{" "}
              and we&apos;ll get it sorted.
            </p>
          </div>
        ) : (
          <p className="text-sm text-[var(--danger)]">
            Couldn&apos;t save that just yet.{" "}
            <button className="underline" onClick={() => onSubmit(mode)}>
              Retry
            </button>
          </p>
        ))}
    </div>
  );
}
