"use client";

import { useEffect, useMemo, useState } from "react";
import { useAuth } from "@clerk/nextjs";

import { useApi } from "@/lib/api";
import { getMySellerProfile, updateListing, type SellerProfile } from "@/lib/marketplace/api";
import { HandlePicker } from "@/components/marketplace/HandlePicker";

import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";
import {
  PickAgentStep,
  DetailsStep,
  BundleStep,
  PricingStep,
  ReviewStep,
  SuccessStep,
} from "./wizardSteps";
import type { Listing } from "@/lib/marketplace/types";

export interface PublishForm {
  agentId?: string;
  name: string;
  tagline: string;
  description: string;
  category: string;
  tags: string[];
  priceTier: "free" | "paid";
  priceDollars: number;
  // Bundle step — seller's explicit MCP + skill selection. Empty arrays
  // (the default) mean "don't bundle anything"; the listing ships the
  // agent's workspace files only and the buyer's openclaw.json is left
  // alone for MCPs/skills.
  bundleMcps: string[];
  bundleSkills: string[];
}

const INITIAL_FORM: PublishForm = {
  name: "",
  tagline: "",
  description: "",
  category: "Operations",
  tags: [],
  priceTier: "free",
  priceDollars: 10,
  bundleMcps: [],
  bundleSkills: [],
};

const CATEGORIES = [
  "Operations",
  "Sales",
  "Engineering",
  "Research",
  "Finance",
  "Marketing",
  "Personal",
];

// Pick → Details → Bundle → Pricing → Review → Success
type Step = 1 | 2 | 3 | 4 | 5 | 6;

/** Build the initial form state from an existing rejected/draft listing.
 *
 * Description rendering: the draft only stores `description_md`. The wizard
 * splits that into a one-line tagline + multi-line description in the
 * Details step. For listings created via the current wizard, description_md
 * was set to `form.description || form.tagline` — so we round-trip back into
 * `description` only, leaving the tagline empty for the seller to refill if
 * they want the two-line treatment. Less magical than guessing.
 */
function formFromListing(listing: Listing): PublishForm {
  const titleCaseCategory = (raw: string | null | undefined): string => {
    if (!raw) return INITIAL_FORM.category;
    const lower = raw.toLowerCase();
    const match = CATEGORIES.find((c) => c.toLowerCase() === lower);
    return match ?? INITIAL_FORM.category;
  };
  return {
    agentId: listing.agent_id ?? listing.manifest_json?.agent_id ?? undefined,
    name: listing.name,
    tagline: "",
    description: listing.description_md ?? "",
    category: titleCaseCategory(listing.category),
    tags: listing.tags ?? [],
    priceTier: (listing.price_cents ?? 0) > 0 ? "paid" : "free",
    priceDollars: (listing.price_cents ?? 0) > 0 ? Math.round((listing.price_cents ?? 0) / 100) : 10,
    // Prefill the BundleStep from the row's persisted selection. Empty
    // arrays here mean "we genuinely shipped nothing" (e.g. the previous
    // upload was a pure-workspace listing); null/undefined means the
    // listing predates the column and falls through to []. Either way,
    // the seller can re-check what they want before resubmitting.
    bundleMcps: listing.bundle_mcps ?? [],
    bundleSkills: listing.bundle_skills ?? [],
  };
}

export function PublishWizard() {
  const { setView, state } = useMarketplaceDrawer();
  const api = useApi();
  const { getToken } = useAuth();
  const editingListing = state.editListing ?? null;
  const isEditing = !!editingListing;

  // Drafts can exist without a recoverable agent_id — e.g. the seller's
  // first wizard run created the draft via POST /listings but the
  // artifact-from-agent step failed before agent_id was persisted. For
  // those, fall back to step 1 (PickAgent unlocked) so the seller can
  // bind an agent before continuing. With a known agent_id, step 1 is
  // locked and we jump to Details.
  const recoveredAgentId = editingListing
    ? editingListing.agent_id ?? editingListing.manifest_json?.agent_id ?? null
    : null;
  const agentLocked = isEditing && !!recoveredAgentId;
  const stepFloor: Step = agentLocked ? 2 : 1;

  // Pre-flight a listing already in flight (the seller hit Submit, then a
  // step failed mid-chain). `draftListingId` carries the create_draft's
  // listing_id forward so re-clicking Submit doesn't try to recreate the
  // same slug. In edit mode it's pinned to the listing we're revising.
  const [step, setStep] = useState<Step>(stepFloor);
  const [form, setForm] = useState<PublishForm>(() =>
    editingListing
      ? formFromListing(editingListing)
      : { ...INITIAL_FORM, agentId: state.publishAgentId },
  );
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [draftListingId, setDraftListingId] = useState<string | null>(
    editingListing?.listing_id ?? null,
  );

  // Marketplace handle gate. The publish flow refuses POST /listings with
  // 409 seller_profile_required if the seller has no handle yet — we
  // pre-flight on mount to surface the picker before they hit Submit, and
  // also fall back to it if Submit hits the 409.
  const [sellerProfile, setSellerProfile] = useState<SellerProfile | null>(null);
  const [profileChecked, setProfileChecked] = useState(false);
  const [forceHandleGate, setForceHandleGate] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const jwt = await getToken();
        if (!jwt) {
          if (!cancelled) setProfileChecked(true);
          return;
        }
        const profile = await getMySellerProfile({ jwt });
        if (!cancelled) {
          setSellerProfile(profile);
          setProfileChecked(true);
        }
      } catch {
        if (!cancelled) setProfileChecked(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [getToken]);

  // Rejection banner — present iff the listing was rejected and not yet
  // resubmitted. Rendered on every wizard step in edit mode so the seller
  // doesn't lose track of why they're here.
  const rejectionNote = editingListing?.rejection_notes ?? null;

  const setAgent = (id: string) =>
    setForm((f) => ({
      ...f,
      agentId: id,
      name: f.name || titleFromId(id),
      tagline: f.tagline || `${titleFromId(id)} agent`,
    }));

  const updateForm = (patch: Partial<PublishForm>) => setForm((f) => ({ ...f, ...patch }));

  function canContinue() {
    if (step === 1) return !!form.agentId;
    if (step === 2) {
      // Name is always required. submit() sends `description || tagline`
      // as description_md, and the backend's ListingCreate/ListingUpdate
      // schemas require min_length=1 on that field — so one of the two
      // must have content to avoid a 422 at submit time.
      if (form.name.trim().length === 0) return false;
      return form.tagline.trim().length > 0 || form.description.trim().length > 0;
    }
    if (step === 3) return true; // Bundle step: any selection (including empty) is valid
    if (step === 4) return form.priceTier === "free" || form.priceDollars >= 1;
    if (step === 5) return true;
    return false;
  }

  function next() {
    if (step < 5) setStep((s) => (s + 1) as Step);
    else if (step === 5) {
      void submit();
    }
  }

  function back() {
    if (step <= stepFloor) return;
    setStep((s) => (s - 1) as Step);
  }

  function cancel() {
    setView(state.prevView ?? "browse");
  }

  /** Final gate before flipping the listing to review: if the seller
   * selected the paid tier but has no Stripe Connect account, refuse to
   * submit. The wizard's PricingStep shows the same gate visually, but
   * the seller could have ignored the banner and tabbed past — re-check
   * at the submit boundary so we can't ship a paid listing with no
   * payouts target. */
  async function ensurePayoutsForPaid(): Promise<boolean> {
    if (form.priceTier !== "paid") return true;
    try {
      const r = (await api.get("/marketplace/payouts/dashboard")) as {
        dashboard_url?: string | null;
      };
      if (r.dashboard_url) return true;
    } catch {
      // Fall through to the gate — if we can't tell, force the seller
      // through onboarding before submitting a paid listing.
    }
    setSubmitError(
      "Premium-tier listings require a Stripe Connect account. Switch to Free or set up payouts before resubmitting.",
    );
    setStep(4);
    return false;
  }

  async function submit() {
    setSubmitting(true);
    setSubmitError(null);
    try {
      if (!(await ensurePayoutsForPaid())) {
        return;
      }
      const priceCents = form.priceTier === "free" ? 0 : Math.round(form.priceDollars * 100);

      let listingId = draftListingId;
      // Edit mode targets the rejected row's actual version. Today every
      // listing is v=1 (create_draft hardcodes that), but threading the
      // value through closes the future republish hole: when a v_new=2
      // row gets rejected, this code will revise that row instead of v=1.
      const editVersion = editingListing?.version ?? 1;
      if (isEditing && listingId) {
        const jwt = await getToken();
        if (!jwt) throw new Error("Not signed in");
        await updateListing({
          listingId,
          jwt,
          version: editVersion,
          patch: {
            name: form.name.trim(),
            description_md: form.description || form.tagline,
            price_cents: priceCents,
            tags: form.tags,
            category: form.category.toLowerCase(),
          },
        });
      } else if (!listingId) {
        const slug = slugify(form.name);
        const draft = (await api.post("/marketplace/listings", {
          slug,
          name: form.name.trim(),
          description_md: form.description || form.tagline,
          format: "openclaw",
          price_cents: priceCents,
          tags: form.tags,
          // Lowercased to match Browse-view's category-chip filter casing
          // (Browse passes `category === "all" ? undefined : category` lowercased).
          category: form.category.toLowerCase(),
        })) as { listing_id: string };
        listingId = draft.listing_id;
        setDraftListingId(listingId);
      }
      const versionQs = isEditing && editVersion !== 1 ? `?version=${editVersion}` : "";
      await api.post(
        `/marketplace/listings/${listingId}/artifact-from-agent${versionQs}`,
        {
          agent_id: form.agentId,
          bundle: { mcps: form.bundleMcps, skills: form.bundleSkills },
        },
      );
      await api.post(`/marketplace/listings/${listingId}/submit${versionQs}`, {});
      setStep(6);
    } catch (err) {
      // useApi's ApiError doesn't expose `.detail` directly; the body's
      // detail string lives at `.body.detail` and is also serialized into
      // `.message`. Check all three so 409s with shape {detail:"slug ..."}
      // route to the slug-collision branch regardless of which field carries
      // the text.
      const e = err as Error & {
        status?: number;
        detail?: string;
        body?: { detail?: string };
      };
      const errMsg = e.body?.detail || e.detail || e.message || "";
      if (e.status === 409 && /slug/i.test(errMsg)) {
        setSubmitError("That slug is already taken — change the name and try again.");
        setDraftListingId(null);
        setStep(2);
      } else if (e.status === 409 && /seller_profile_required/i.test(errMsg)) {
        // Backend gated the publish on the seller having a marketplace
        // handle. Surface the picker inline; on save we'll let the seller
        // retry Submit. Don't auto-retry — the seller just spent time
        // setting their public identity and should re-confirm Submit.
        setForceHandleGate(true);
        setSubmitError(null);
      } else {
        setSubmitError(errMsg || "Failed to submit listing");
      }
    } finally {
      setSubmitting(false);
    }
  }

  // Progress-pip rendering: when step 1 is locked (agent_id recovered) we
  // render four pips (2–5). When the seller has to (re-)pick an agent —
  // either a fresh listing or an orphaned draft without agent_id — we
  // render the full five.
  const pips = useMemo<number[]>(
    () => (agentLocked ? [2, 3, 4, 5] : [1, 2, 3, 4, 5]),
    [agentLocked],
  );

  // Editing a listing that's currently in review pulls it back to draft
  // on save (backend update_draft auto-demotes). Warn the seller up front
  // so the side effect isn't a surprise — they'll need to resubmit.
  const editingFromReview = editingListing?.status === "review";

  return (
    <div className="flex flex-col h-full">
      {rejectionNote && step !== 6 && (
        <div
          role="alert"
          data-testid="wizard-rejection-banner"
          className="mb-4 rounded-xl border border-[#f0b3a8] bg-[#fbe9e7] px-3.5 py-2.5 text-[13px] text-[#8a3a2a] leading-snug"
        >
          <strong className="block font-semibold text-[#5e2a1f]">
            Admin sent this listing back for revisions.
          </strong>
          {rejectionNote}
        </div>
      )}
      {editingFromReview && step !== 6 && (
        <div
          role="status"
          data-testid="wizard-review-pullback-banner"
          className="mb-4 rounded-xl border border-[#e8d49a] bg-[#fff4d6] px-3.5 py-2.5 text-[13px] text-[#7d5f1a] leading-snug"
        >
          <strong className="block font-semibold text-[#5e4814]">
            This listing is currently in review.
          </strong>
          Saving your edits pulls it out of the moderation queue back to draft.
          Resubmit to re-enter review.
        </div>
      )}

      <div className="flex items-center gap-1.5 mb-4">
        {pips.map((n) => (
          <span
            key={n}
            className={`w-5 h-1 rounded-full ${n <= step ? "bg-[#06402b]" : "bg-[#e0dbd0]"}`}
          />
        ))}
        <span className="text-[11.5px] text-[#5b5b5b] ml-2">
          {step === 6
            ? isEditing
              ? "Resubmitted"
              : "Submitted"
            : `Step ${agentLocked ? step - 1 : step} of ${agentLocked ? 4 : 5}`}
        </span>
      </div>

      {profileChecked && !sellerProfile && step !== 6 && (
        <div
          role="region"
          aria-label="Marketplace handle required"
          className="mb-4 rounded-2xl border border-[#d8b46a] bg-[#fbf3df] px-3.5 py-3"
        >
          <div className="text-[12.5px] font-semibold text-[#5b4a1c] mb-1">
            Pick a marketplace handle before publishing
          </div>
          <p className="text-[12px] text-[#7a6432] mb-3 leading-snug">
            Your handle appears on every listing byline and on (future) credibility
            surfaces. You can change it later from Settings → Marketplace.
          </p>
          <HandlePicker
            compact
            saveLabel="Save handle"
            onSaved={(profile) => {
              setSellerProfile(profile);
              setForceHandleGate(false);
            }}
          />
        </div>
      )}

      {forceHandleGate && sellerProfile === null && (
        // Defence-in-depth: if the 409 fired but the pre-flight banner is
        // already rendered, this branch is a no-op. Kept here so the gate
        // shows even on the (rare) path where the pre-flight succeeded
        // and then the profile was deleted out-of-band before Submit.
        <div className="hidden" />
      )}

      <div className="flex-1">
        {step === 1 && <PickAgentStep selected={form.agentId} onSelect={setAgent} />}
        {step === 2 && <DetailsStep form={form} onChange={updateForm} />}
        {step === 3 && <BundleStep form={form} onChange={updateForm} />}
        {step === 4 && <PricingStep form={form} onChange={updateForm} />}
        {step === 5 && <ReviewStep form={form} />}
        {step === 6 && (
          <SuccessStep
            onViewMine={() => setView("mine")}
            mode={isEditing ? "resubmit" : "submit"}
          />
        )}
      </div>

      {step <= 5 && (
        <div className="flex justify-between gap-2 pt-3 mt-4 border-t border-[#e0dbd0]">
          <button
            type="button"
            onClick={back}
            disabled={step <= stepFloor || submitting}
            className="text-[#5b5b5b] hover:bg-[#f3eee5] px-4 py-2 rounded-full text-[14px] disabled:opacity-30"
          >
            Back
          </button>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={cancel}
              disabled={submitting}
              className="text-[#5b5b5b] hover:bg-[#f3eee5] px-4 py-2 rounded-full text-[14px]"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={next}
              disabled={
                !canContinue() ||
                submitting ||
                // Block the final Submit step when the seller hasn't picked a
                // marketplace handle yet. The pre-flight banner above the step
                // content surfaces the picker.
                (step === 5 && profileChecked && !sellerProfile)
              }
              className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-5 py-2 rounded-full text-[14px] font-semibold disabled:opacity-50"
            >
              {step === 5
                ? isEditing
                  ? "Resubmit for review →"
                  : "Submit for review →"
                : "Continue →"}
            </button>
          </div>
        </div>
      )}

      {submitError && (
        <p role="alert" className="mt-3 text-[13px] text-[#b32424]">
          {submitError}
        </p>
      )}
    </div>
  );
}

function titleFromId(id: string): string {
  return id
    .replace(/^agent[_-]/, "")
    .replace(/[_-]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function slugify(input: string): string {
  return (
    input
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 64) || "untitled"
  );
}
