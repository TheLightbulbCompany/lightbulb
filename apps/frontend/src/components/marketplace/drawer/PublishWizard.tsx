"use client";

import { useState } from "react";

import { useApi } from "@/lib/api";

import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";
import {
  PickAgentStep,
  DetailsStep,
  BundleStep,
  PricingStep,
  ReviewStep,
  SuccessStep,
} from "./wizardSteps";

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

// Pick → Details → Bundle → Pricing → Review → Success
type Step = 1 | 2 | 3 | 4 | 5 | 6;

export function PublishWizard() {
  const { setView, state } = useMarketplaceDrawer();
  const api = useApi();
  const [step, setStep] = useState<Step>(1);
  const [form, setForm] = useState<PublishForm>({
    ...INITIAL_FORM,
    agentId: state.publishAgentId,
  });
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [draftListingId, setDraftListingId] = useState<string | null>(null);

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
    if (step === 2) return form.name.trim().length > 0 && form.tagline.trim().length > 0;
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
    if (step > 1) setStep((s) => (s - 1) as Step);
  }

  function cancel() {
    setView(state.prevView ?? "browse");
  }

  async function submit() {
    setSubmitting(true);
    setSubmitError(null);
    try {
      let listingId = draftListingId;
      if (!listingId) {
        const slug = slugify(form.name);
        const draft = (await api.post("/marketplace/listings", {
          slug,
          name: form.name.trim(),
          description_md: form.description || form.tagline,
          format: "openclaw",
          price_cents: form.priceTier === "free" ? 0 : Math.round(form.priceDollars * 100),
          tags: form.tags,
          // Lowercased to match Browse-view's category-chip filter casing
          // (Browse passes `category === "all" ? undefined : category` lowercased).
          category: form.category.toLowerCase(),
        })) as { listing_id: string };
        listingId = draft.listing_id;
        setDraftListingId(listingId);
      }
      await api.post(`/marketplace/listings/${listingId}/artifact-from-agent`, {
        agent_id: form.agentId,
        bundle: { mcps: form.bundleMcps, skills: form.bundleSkills },
      });
      await api.post(`/marketplace/listings/${listingId}/submit`, {});
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
      } else {
        setSubmitError(errMsg || "Failed to submit listing");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-1.5 mb-4">
        {[1, 2, 3, 4, 5].map((n) => (
          <span
            key={n}
            className={`w-5 h-1 rounded-full ${n <= step ? "bg-[#06402b]" : "bg-[#e0dbd0]"}`}
          />
        ))}
        <span className="text-[11.5px] text-[#5b5b5b] ml-2">
          {step === 6 ? "Submitted" : `Step ${step} of 5`}
        </span>
      </div>

      <div className="flex-1">
        {step === 1 && <PickAgentStep selected={form.agentId} onSelect={setAgent} />}
        {step === 2 && <DetailsStep form={form} onChange={updateForm} />}
        {step === 3 && <BundleStep form={form} onChange={updateForm} />}
        {step === 4 && <PricingStep form={form} onChange={updateForm} />}
        {step === 5 && <ReviewStep form={form} />}
        {step === 6 && <SuccessStep onViewMine={() => setView("mine")} />}
      </div>

      {step <= 5 && (
        <div className="flex justify-between gap-2 pt-3 mt-4 border-t border-[#e0dbd0]">
          <button
            type="button"
            onClick={back}
            disabled={step === 1 || submitting}
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
              disabled={!canContinue() || submitting}
              className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-5 py-2 rounded-full text-[14px] font-semibold disabled:opacity-50"
            >
              {step === 5 ? "Submit for review →" : "Continue →"}
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
