"use client";

import { useAuth } from "@clerk/nextjs";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  checkHandleAvailability,
  getMySellerProfile,
  SellerProfileWriteError,
  upsertMySellerProfile,
  type HandleAvailability,
  type SellerProfile,
} from "@/lib/marketplace/api";
import { useMarketplaceWriteGate } from "@/lib/marketplace/useMarketplaceWriteGate";
import { MarketplaceRestrictedView } from "@/components/marketplace/RestrictedView";

/**
 * Marketplace handle picker — shared between the settings page (standalone
 * editor) and the publish wizard (gate before POST /listings).
 *
 * Handle format mirrors the backend: `[a-z0-9_-]{3,32}`. Inline availability
 * check is debounced 350ms so the input stays responsive while the user
 * is still typing.
 *
 * On a successful save the parent gets the new profile via `onSaved` so it
 * can advance / dismiss without re-reading. Empty `initialProfile` means
 * "no profile yet" — used by the publish-wizard gate.
 */

const HANDLE_RE = /^[a-z0-9_-]{3,32}$/;

type Status =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "available" }
  | { kind: "unavailable"; reason: HandleAvailability["reason"] }
  | { kind: "error"; message: string };

export interface HandlePickerProps {
  /** Optional existing profile to pre-fill the form. */
  initialProfile?: SellerProfile | null;
  /** Called with the saved profile after a successful PUT. */
  onSaved?: (profile: SellerProfile) => void;
  /** Cancel / dismiss callback (used by the publish-wizard gate). */
  onCancel?: () => void;
  /** Render the form in compact mode (no bio, smaller padding). */
  compact?: boolean;
  /** Label for the primary action button. */
  saveLabel?: string;
}

export function HandlePicker({
  initialProfile = null,
  onSaved,
  onCancel,
  compact = false,
  saveLabel = "Save",
}: HandlePickerProps) {
  const { getToken } = useAuth();
  const { restricted } = useMarketplaceWriteGate();
  const [handle, setHandle] = useState(initialProfile?.handle ?? "");
  const [displayName, setDisplayName] = useState(initialProfile?.display_name ?? "");
  const [bio, setBio] = useState(initialProfile?.bio ?? "");
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const requestSeqRef = useRef(0);

  const normalised = handle.trim().toLowerCase();
  const isUnchanged = !!initialProfile && initialProfile.handle === normalised;

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (!normalised) {
      setStatus({ kind: "idle" });
      return;
    }
    if (isUnchanged) {
      setStatus({ kind: "available" });
      return;
    }
    if (!HANDLE_RE.test(normalised)) {
      setStatus({ kind: "unavailable", reason: "format" });
      return;
    }

    setStatus({ kind: "checking" });
    const mySeq = ++requestSeqRef.current;
    debounceRef.current = setTimeout(async () => {
      try {
        const jwt = await getToken();
        if (!jwt) throw new Error("Not signed in");
        const result = await checkHandleAvailability({ jwt, handle: normalised });
        if (mySeq !== requestSeqRef.current) return;
        if (result.available) {
          setStatus({ kind: "available" });
        } else {
          setStatus({ kind: "unavailable", reason: result.reason });
        }
      } catch (err) {
        if (mySeq !== requestSeqRef.current) return;
        setStatus({
          kind: "error",
          message: err instanceof Error ? err.message : "check failed",
        });
      }
    }, 350);

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [normalised, isUnchanged, getToken]);

  const handleChange = useCallback((next: string) => {
    setServerError(null);
    setHandle(next.replace(/^@/, "")); // tolerate paste with leading @
  }, []);

  const canSave =
    !!normalised &&
    HANDLE_RE.test(normalised) &&
    (status.kind === "available" || (isUnchanged && status.kind !== "checking")) &&
    !submitting;

  async function save() {
    if (!canSave) return;
    setSubmitting(true);
    setServerError(null);
    try {
      const jwt = await getToken();
      if (!jwt) throw new Error("Not signed in");
      const profile = await upsertMySellerProfile({
        jwt,
        payload: {
          handle: normalised,
          display_name: displayName.trim() || null,
          bio: bio.trim() || null,
        },
      });
      onSaved?.(profile);
    } catch (err) {
      if (err instanceof SellerProfileWriteError) {
        if (err.reason === "handle_taken") {
          setStatus({ kind: "unavailable", reason: "taken" });
        } else if (err.reason === "handle_reserved") {
          setStatus({ kind: "unavailable", reason: "reserved" });
        } else if (err.reason === "handle_format") {
          setStatus({ kind: "unavailable", reason: "format" });
        } else {
          setServerError("Could not save your profile. Try again.");
        }
      } else {
        setServerError(err instanceof Error ? err.message : "save failed");
      }
    } finally {
      setSubmitting(false);
    }
  }

  const padding = compact ? "p-4" : "p-5";
  const gap = compact ? "gap-3" : "gap-4";

  // Non-admin org members can't set the seller handle — backend PUT
  // /seller/me 403s for them. Render the restricted state instead of
  // an input that can't successfully save.
  if (restricted) {
    return (
      <MarketplaceRestrictedView
        title="Marketplace handle restricted"
        desc="Only org admins can set the marketplace seller handle. Ask your org admin to do this."
      />
    );
  }

  return (
    <div className={`flex flex-col ${gap} ${padding} rounded-2xl border border-[#e0dbd0] bg-white`}>
      <div>
        <label
          htmlFor="marketplace-handle"
          className="block text-[12.5px] font-semibold text-[#1a1a1a] mb-1.5"
        >
          Marketplace handle
        </label>
        <div className="flex items-center rounded-lg border border-[#cfc7b8] bg-[#faf7f2] focus-within:border-[#06402b] focus-within:ring-1 focus-within:ring-[#06402b]/20">
          <span className="pl-3 pr-1 text-[#8a8478] text-[14px] select-none">@</span>
          <input
            id="marketplace-handle"
            type="text"
            value={handle}
            onChange={(e) => handleChange(e.target.value)}
            placeholder="your-handle"
            autoComplete="off"
            spellCheck={false}
            className="flex-1 bg-transparent py-2 pr-3 text-[14px] text-[#1a1a1a] placeholder:text-[#b5af9f] outline-none"
            maxLength={32}
          />
          <span className="pr-3 text-[11.5px] min-w-[80px] text-right">
            <AvailabilityIndicator status={status} />
          </span>
        </div>
        <p className="mt-1.5 text-[11.5px] text-[#8a8478]">
          3–32 chars; lowercase letters, digits, hyphens, underscores. Public on every
          listing byline.
        </p>
      </div>

      <div>
        <label
          htmlFor="marketplace-display-name"
          className="block text-[12.5px] font-semibold text-[#1a1a1a] mb-1.5"
        >
          Display name <span className="text-[#8a8478] font-normal">(optional)</span>
        </label>
        <input
          id="marketplace-display-name"
          type="text"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="What people call you"
          className="w-full rounded-lg border border-[#cfc7b8] bg-[#faf7f2] py-2 px-3 text-[14px] text-[#1a1a1a] placeholder:text-[#b5af9f] focus:border-[#06402b] focus:outline-none focus:ring-1 focus:ring-[#06402b]/20"
          maxLength={64}
        />
      </div>

      {!compact && (
        <div>
          <label
            htmlFor="marketplace-bio"
            className="block text-[12.5px] font-semibold text-[#1a1a1a] mb-1.5"
          >
            Bio <span className="text-[#8a8478] font-normal">(optional, up to 280)</span>
          </label>
          <textarea
            id="marketplace-bio"
            value={bio}
            onChange={(e) => setBio(e.target.value)}
            placeholder="Builders should know..."
            rows={3}
            className="w-full rounded-lg border border-[#cfc7b8] bg-[#faf7f2] py-2 px-3 text-[14px] text-[#1a1a1a] placeholder:text-[#b5af9f] focus:border-[#06402b] focus:outline-none focus:ring-1 focus:ring-[#06402b]/20 resize-none"
            maxLength={280}
          />
        </div>
      )}

      {serverError && (
        <div className="rounded-lg border border-[#f0b3a8] bg-[#fbe9e7] px-3 py-2 text-[12.5px] text-[#8a3a2a]">
          {serverError}
        </div>
      )}

      <div className="flex items-center justify-end gap-2">
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            disabled={submitting}
            className="text-[12.5px] text-[#5b5b5b] hover:text-[#1a1a1a] px-3.5 py-1.5"
          >
            Cancel
          </button>
        )}
        <button
          type="button"
          onClick={save}
          disabled={!canSave}
          className="bg-[#06402b] text-white px-4 py-1.5 rounded-full text-[12.5px] font-semibold hover:bg-[#0a5a3d] disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {submitting ? "Saving…" : saveLabel}
        </button>
      </div>
    </div>
  );
}

function AvailabilityIndicator({ status }: { status: Status }) {
  if (status.kind === "idle") return <span className="text-[#b5af9f]">—</span>;
  if (status.kind === "checking") return <span className="text-[#8a8478]">checking…</span>;
  if (status.kind === "available") return <span className="text-[#06402b]">available</span>;
  if (status.kind === "error") return <span className="text-[#8a3a2a]">offline</span>;
  // unavailable
  if (status.reason === "format") return <span className="text-[#8a3a2a]">format</span>;
  if (status.reason === "reserved") return <span className="text-[#8a3a2a]">reserved</span>;
  return <span className="text-[#8a3a2a]">taken</span>;
}

/** Fetch the current user's profile (or null) — convenience for callers
 * who only want the eager pre-flight without a state library. */
export async function fetchSellerProfile(getToken: () => Promise<string | null>) {
  const jwt = await getToken();
  if (!jwt) return null;
  return getMySellerProfile({ jwt });
}
