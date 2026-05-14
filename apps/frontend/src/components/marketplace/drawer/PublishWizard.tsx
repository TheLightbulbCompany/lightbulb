"use client";

import { useEffect, useState } from "react";

import { useApi, ApiError } from "@/lib/api";

import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";
import {
  PickAgentStep,
  DetailsStep,
  BundleStep,
  PricingStep,
  ReviewStep,
  SuccessStep,
  ClaimUsernameStep,
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

// Step 0 = ClaimUsername (rendered only when the user hasn't claimed a
// handle yet). 1..6 = the existing publish chain. Pre-flight on mount
// decides whether step 0 is rendered.
type Step = 0 | 1 | 2 | 3 | 4 | 5 | 6;

interface SellerState {
  username: string;
  last_changed_at: string | null;
  cooldown_ends_at: string | null;
}

export function PublishWizard() {
  const { setView, state } = useMarketplaceDrawer();
  const api = useApi();
  // Start at -1 sentinel until the seller-username preflight resolves so
  // we don't flash either step before we know whether to show Claim.
  const [step, setStep] = useState<Step | null>(null);
  const [form, setForm] = useState<PublishForm>({
    ...INITIAL_FORM,
    agentId: state.publishAgentId,
  });
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [draftListingId, setDraftListingId] = useState<string | null>(null);

  // Claim-username state — only used when the user hasn't claimed a
  // handle yet. Once claimed, the wizard advances to step 1 and these
  // are never touched again.
  const [usernameInput, setUsernameInput] = useState("");
  const [usernameError, setUsernameError] = useState<string | null>(null);
  const [claiming, setClaiming] = useState(false);

  // Preflight: hit GET /sellers/me. 200 -> already claimed, jump to 1.
  // 404 -> needs claim, start at 0. Other errors -> assume already
  // claimed (degrade gracefully — the backend gate will still reject
  // create_listing with 412 so we never produce orphan rows).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await api.get("/sellers/me");
        if (!cancelled) setStep(1);
      } catch (err) {
        const e = err as ApiError;
        if (!cancelled) setStep(e?.status === 404 ? 0 : 1);
      }
    })();
    return () => {
      cancelled = true;
    };
    // api is stable per useApi memoization, so this only runs once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
    if (step === null || step === 0) return;
    if (step < 5) setStep((s) => ((s ?? 1) + 1) as Step);
    else if (step === 5) {
      void submit();
    }
  }

  function back() {
    if (step !== null && step > 1) setStep((s) => ((s ?? 1) - 1) as Step);
  }

  function cancel() {
    setView(state.prevView ?? "browse");
  }

  async function claimUsername() {
    setClaiming(true);
    setUsernameError(null);
    try {
      (await api.post("/sellers/me/username", {
        username: usernameInput.trim(),
      })) as SellerState;
      // Move on to the standard wizard chain.
      setStep(1);
    } catch (err) {
      const e = err as ApiError;
      const body = e?.body as { detail?: { error?: string; cooldown_ends_at?: string } } | null;
      const code = body?.detail?.error ?? "";
      if (code === "taken") {
        setUsernameError("That handle is taken. Pick another.");
      } else if (code === "reserved") {
        setUsernameError("That handle is reserved. Pick another.");
      } else if (code === "invalid_format") {
        setUsernameError(
          "Use 3-30 lowercase letters, digits, hyphens, or underscores.",
        );
      } else if (code === "cooldown_active") {
        const next = body?.detail?.cooldown_ends_at;
        setUsernameError(
          next
            ? `You can change your handle again on ${new Date(next).toLocaleDateString()}.`
            : "You can only change your handle every 90 days.",
        );
      } else {
        setUsernameError(e?.message || "Failed to claim handle");
      }
    } finally {
      setClaiming(false);
    }
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
        body?: { detail?: string | { error?: string } };
      };
      // Defensive: a 412 from the backend means the username gate
      // fired (race -- pre-flight saw a claim that vanished, or the
      // user's row hasn't replicated yet). Send them back to step 0.
      if (e.status === 412) {
        setStep(0);
        setSubmitError("Claim a seller handle before publishing.");
        return;
      }
      const detailBody = e.body?.detail;
      const errMsg =
        typeof detailBody === "string"
          ? detailBody
          : typeof detailBody === "object" && detailBody?.error
            ? detailBody.error
            : (e.detail || e.message || "");
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

  if (step === null) {
    return <div className="text-[13px] text-[#5b5b5b]">Loading…</div>;
  }

  if (step === 0) {
    return (
      <div className="flex flex-col h-full">
        <div className="flex-1">
          <ClaimUsernameStep
            value={usernameInput}
            onChange={(v) => {
              setUsernameInput(v);
              setUsernameError(null);
            }}
            error={usernameError}
            onSubmit={() => void claimUsername()}
            submitting={claiming}
          />
        </div>
        <div className="flex justify-end pt-3 mt-4 border-t border-[#e0dbd0]">
          <button
            type="button"
            onClick={cancel}
            disabled={claiming}
            className="text-[#5b5b5b] hover:bg-[#f3eee5] px-4 py-2 rounded-full text-[14px]"
          >
            Cancel
          </button>
        </div>
      </div>
    );
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
