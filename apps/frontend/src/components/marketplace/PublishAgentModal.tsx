"use client";

import { useCallback, useMemo, useState } from "react";
import { Loader2, AlertCircle, ExternalLink, CheckCircle2 } from "lucide-react";

import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useApi, BACKEND_URL } from "@/lib/api";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types matching apps/backend/schemas/marketplace.py
// ---------------------------------------------------------------------------

interface ListingCreateResponse {
  listing_id: string;
  slug: string;
  [k: string]: unknown;
}

export interface PublishAgentModalProps {
  agent: {
    agent_id: string;
    name?: string;
    description_md?: string;
  };
  open: boolean;
  onClose: () => void;
  onPublished?: (listingId: string) => void;
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/;

export function slugify(input: string): string {
  return input
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

function validateSlug(slug: string): string | null {
  if (slug.length < 2 || slug.length > 64) {
    return "Slug must be 2-64 characters";
  }
  if (!SLUG_RE.test(slug)) {
    return "Slug must be lowercase letters, digits, and hyphens (no leading/trailing hyphen)";
  }
  return null;
}

function validateName(name: string): string | null {
  const t = name.trim();
  if (t.length < 2 || t.length > 80) return "Name must be 2-80 characters";
  return null;
}

function validateDescription(d: string): string | null {
  if (d.length < 1) return "Description is required";
  if (d.length > 4096) return "Description must be 4096 characters or fewer";
  return null;
}

function parseTags(raw: string): string[] {
  return raw
    .split(",")
    .map((t) => t.trim().toLowerCase())
    .filter((t) => t.length > 0);
}

// ---------------------------------------------------------------------------
// Storefront URL — pin to dev/prod by API host. The marketplace storefront
// is path-based (/marketplace/...) on the same isol8.co frontend, not a
// separate subdomain. See apps/frontend/src/app/marketplace/ for the routes.
// ---------------------------------------------------------------------------

export function storefrontUrlForSlug(slug: string): string {
  // Examples:
  //   https://api-dev.isol8.co/api/v1     → https://dev.isol8.co/marketplace/listing/<slug>
  //   https://api-staging.isol8.co/api/v1 → https://staging.isol8.co/marketplace/listing/<slug>
  //   https://api.isol8.co/api/v1         → https://isol8.co/marketplace/listing/<slug>
  //   http://localhost:8000/api/v1        → http://localhost:3000/marketplace/listing/<slug>
  const path = `/marketplace/listing/${encodeURIComponent(slug)}`;
  try {
    const url = new URL(BACKEND_URL);
    const host = url.hostname;
    if (host === "localhost" || host === "127.0.0.1") {
      return `http://localhost:3000${path}`;
    }
    // Extract env from `api[-env].isol8.co` and produce `[env.]isol8.co`.
    // Prod (api.isol8.co) → no env segment. Non-prod hyphenated suffix becomes
    // a dotted subdomain matching the existing frontend convention
    // (dev.isol8.co, staging.isol8.co).
    const m = host.match(/^api(?:-([a-z]+))?\.isol8\.co$/);
    if (m) {
      const env = m[1];
      const frontHost = env ? `${env}.isol8.co` : "isol8.co";
      return `https://${frontHost}${path}`;
    }
    return `https://isol8.co${path}`;
  } catch {
    return `https://isol8.co${path}`;
  }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

type Step = "form" | "submitting" | "success" | "error";

export function PublishAgentModal({ agent, open, onClose, onPublished }: PublishAgentModalProps) {
  const api = useApi();

  const [step, setStep] = useState<Step>("form");
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [publishedListingId, setPublishedListingId] = useState<string | null>(null);
  // Tracks the listing_id once step 1 (create) succeeds, so a retry after a
  // step-2/step-3 failure resumes from where we left off rather than
  // creating a duplicate draft (which would then 409 on slug uniqueness).
  const [draftListingId, setDraftListingId] = useState<string | null>(null);

  // Form state
  const initialSlug = useMemo(() => slugify(agent.name || agent.agent_id), [agent.name, agent.agent_id]);
  const [slug, setSlug] = useState(initialSlug);
  const [name, setName] = useState(agent.name || "");
  const [descriptionMd, setDescriptionMd] = useState(agent.description_md || "");
  const [priceDollars, setPriceDollars] = useState("0");
  const [tagsRaw, setTagsRaw] = useState("");

  // Inline field validation (only shown after the user has touched a field)
  const [touched, setTouched] = useState<Record<string, boolean>>({});

  const slugError = validateSlug(slug);
  const nameError = validateName(name);
  const descError = validateDescription(descriptionMd);
  const priceCents = useMemo(() => {
    const n = Number(priceDollars);
    if (Number.isNaN(n) || n < 0) return -1;
    return Math.round(n * 100);
  }, [priceDollars]);
  const priceError =
    priceCents < 0
      ? "Price must be a non-negative number"
      : priceCents > 2000
        ? "Price cap is $20.00 (2000 cents) for v0"
        : null;
  const tags = useMemo(() => parseTags(tagsRaw), [tagsRaw]);
  const tagsError = tags.length > 5 ? "Max 5 tags" : null;

  const formInvalid = Boolean(slugError || nameError || descError || priceError || tagsError);

  const handleSubmit = useCallback(async () => {
    if (formInvalid) {
      setTouched({ slug: true, name: true, description: true, price: true, tags: true });
      return;
    }
    setStep("submitting");
    setSubmitError(null);

    // Step 1: create draft listing (skipped on retry — draftListingId persists)
    let listingId = draftListingId;
    if (!listingId) {
      try {
        const listing = (await api.post("/marketplace/listings", {
          slug,
          name: name.trim(),
          description_md: descriptionMd,
          format: "openclaw",
          price_cents: priceCents,
          tags,
        })) as ListingCreateResponse;
        listingId = listing.listing_id;
        setDraftListingId(listingId);
      } catch (err) {
        const e = err as Error & { status?: number; detail?: string };
        let msg = e.detail || e.message || "Failed to create listing";
        if (e.status === 409) {
          msg = "That slug is already taken — try another.";
        }
        setSubmitError(msg);
        // Stay on form so the seller can edit slug; no draft was created.
        setStep("form");
        return;
      }
    }

    // Step 2: snapshot agent EFS dir into listing artifact.
    try {
      await api.post(`/marketplace/listings/${listingId}/artifact-from-agent`, {
        agent_id: agent.agent_id,
      });
    } catch (err) {
      const e = err as Error & { status?: number; detail?: string };
      let msg = e.detail || e.message || "Failed to snapshot agent";
      if (e.status === 404) {
        msg = "Agent not found on disk. Try again, or recreate the agent.";
      }
      setSubmitError(msg);
      setStep("error");
      return;
    }

    // Step 3: submit for review
    try {
      await api.post(`/marketplace/listings/${listingId}/submit`, {});
    } catch (err) {
      const e = err as Error & { status?: number; detail?: string };
      const msg = e.detail || e.message || "Failed to submit for review";
      setSubmitError(msg);
      setStep("error");
      return;
    }

    setPublishedListingId(listingId);
    setStep("success");
    onPublished?.(listingId);
  }, [
    api,
    formInvalid,
    slug,
    name,
    descriptionMd,
    priceCents,
    tags,
    agent.agent_id,
    draftListingId,
    onPublished,
  ]);

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (step === "submitting") return; // don't close mid-submit
      if (!next) onClose();
    },
    [onClose, step],
  );

  const isSubmitting = step === "submitting";

  return (
    <AlertDialog open={open} onOpenChange={handleOpenChange}>
      <AlertDialogContent className="max-w-xl bg-white text-[#1a1a1a] border-[#e0dbd0]">
        <AlertDialogHeader>
          <AlertDialogTitle className="text-[#1a1a1a]">
            Publish to marketplace
          </AlertDialogTitle>
          <AlertDialogDescription className="text-[#5a5549]">
            Snapshot {agent.name || agent.agent_id} and submit it as a marketplace listing for review.
          </AlertDialogDescription>
        </AlertDialogHeader>

        {(step === "form" || step === "submitting" || step === "error") && (
          <div className="space-y-4 py-2 max-h-[60vh] overflow-y-auto">
            {/* Slug */}
            <div>
              <label className="block text-xs font-medium text-[#5a5549] mb-1" htmlFor="publish-slug">
                Slug (storefront URL)
              </label>
              <Input
                id="publish-slug"
                value={slug}
                onChange={(e) => setSlug(e.target.value)}
                onBlur={() => setTouched((t) => ({ ...t, slug: true }))}
                disabled={isSubmitting}
                aria-invalid={touched.slug && Boolean(slugError)}
                aria-describedby="publish-slug-error"
              />
              {touched.slug && slugError && (
                <p id="publish-slug-error" className="mt-1 text-xs text-[#dc2626] flex items-center gap-1">
                  <AlertCircle className="h-3 w-3" /> {slugError}
                </p>
              )}
            </div>

            {/* Name */}
            <div>
              <label className="block text-xs font-medium text-[#5a5549] mb-1" htmlFor="publish-name">
                Listing name
              </label>
              <Input
                id="publish-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                onBlur={() => setTouched((t) => ({ ...t, name: true }))}
                disabled={isSubmitting}
                aria-invalid={touched.name && Boolean(nameError)}
              />
              {touched.name && nameError && (
                <p className="mt-1 text-xs text-[#dc2626] flex items-center gap-1">
                  <AlertCircle className="h-3 w-3" /> {nameError}
                </p>
              )}
            </div>

            {/* Description */}
            <div>
              <label className="block text-xs font-medium text-[#5a5549] mb-1" htmlFor="publish-description">
                Storefront description (markdown)
              </label>
              <textarea
                id="publish-description"
                value={descriptionMd}
                onChange={(e) => setDescriptionMd(e.target.value)}
                onBlur={() => setTouched((t) => ({ ...t, description: true }))}
                disabled={isSubmitting}
                rows={5}
                className={cn(
                  "flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  "disabled:cursor-not-allowed disabled:opacity-50",
                )}
                aria-invalid={touched.description && Boolean(descError)}
              />
              <p className="mt-1 text-[10px] text-[#8a8578]">
                {descriptionMd.length} / 4096 characters
              </p>
              {touched.description && descError && (
                <p className="mt-1 text-xs text-[#dc2626] flex items-center gap-1">
                  <AlertCircle className="h-3 w-3" /> {descError}
                </p>
              )}
            </div>

            {/* Price */}
            <div>
              <label className="block text-xs font-medium text-[#5a5549] mb-1" htmlFor="publish-price">
                Price (USD, max $20.00)
              </label>
              <div className="flex items-center gap-1">
                <span className="text-sm text-[#5a5549]">$</span>
                <Input
                  id="publish-price"
                  type="number"
                  inputMode="decimal"
                  min={0}
                  max={20}
                  step={0.01}
                  value={priceDollars}
                  onChange={(e) => setPriceDollars(e.target.value)}
                  onBlur={() => setTouched((t) => ({ ...t, price: true }))}
                  disabled={isSubmitting}
                  aria-invalid={touched.price && Boolean(priceError)}
                />
              </div>
              {touched.price && priceError && (
                <p className="mt-1 text-xs text-[#dc2626] flex items-center gap-1">
                  <AlertCircle className="h-3 w-3" /> {priceError}
                </p>
              )}
            </div>

            {/* Tags */}
            <div>
              <label className="block text-xs font-medium text-[#5a5549] mb-1" htmlFor="publish-tags">
                Tags (comma-separated, max 5)
              </label>
              <Input
                id="publish-tags"
                value={tagsRaw}
                onChange={(e) => setTagsRaw(e.target.value)}
                onBlur={() => setTouched((t) => ({ ...t, tags: true }))}
                disabled={isSubmitting}
                placeholder="research, productivity, devops"
                aria-invalid={touched.tags && Boolean(tagsError)}
              />
              {touched.tags && tagsError && (
                <p className="mt-1 text-xs text-[#dc2626] flex items-center gap-1">
                  <AlertCircle className="h-3 w-3" /> {tagsError}
                </p>
              )}
            </div>

            {/* Bundled-vs-required disclosure (Task #40 v0 surface) */}
            <div className="rounded-md border border-[#e0dbd0] bg-[#f8f6f0] p-3 text-xs text-[#5a5549]">
              <div className="font-medium text-[#1a1a1a] mb-1">What gets published</div>
              <ul className="list-disc pl-4 space-y-0.5">
                <li>Agent identity, prompts, and workflow files (yes)</li>
                <li>Everything in the agent&apos;s workspace directory (yes)</li>
              </ul>
              <div className="font-medium text-[#1a1a1a] mt-2 mb-1">What buyers set up themselves</div>
              <ul className="list-disc pl-4 space-y-0.5">
                <li>Skills your agent uses — buyers register these from their own skill library</li>
                <li>API keys / channel bindings (Telegram, Discord, etc.)</li>
              </ul>
              <p className="mt-2 text-[11px] text-[#8a8578]">
                v0 limitation: the openclaw.json slice (skill registration) is generated manually by the buyer.
                A future release will auto-bundle skill metadata.
              </p>
            </div>

            {submitError && (
              <div
                role="alert"
                className="rounded-md border border-[#dc2626]/40 bg-[#fee2e2] p-3 text-sm text-[#7f1d1d] flex gap-2"
              >
                <AlertCircle className="h-4 w-4 mt-0.5 flex-shrink-0" />
                <span>{submitError}</span>
              </div>
            )}
          </div>
        )}

        {step === "success" && publishedListingId && (
          <div className="space-y-3 py-2">
            <div className="rounded-md border border-emerald-300 bg-emerald-50 p-4 text-sm text-emerald-900 flex gap-2">
              <CheckCircle2 className="h-5 w-5 mt-0.5 flex-shrink-0 text-emerald-600" />
              <div>
                <div className="font-medium">Submitted for review</div>
                <div className="mt-1 text-emerald-800">
                  Your listing is queued for moderation. We&apos;ll notify you when it&apos;s
                  approved and live on the marketplace.
                </div>
                <a
                  href={storefrontUrlForSlug(slug)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-2 inline-flex items-center gap-1 text-emerald-900 underline hover:text-emerald-700"
                >
                  View storefront URL <ExternalLink className="h-3 w-3" />
                </a>
              </div>
            </div>
          </div>
        )}

        <AlertDialogFooter>
          {(step === "form" || step === "submitting" || step === "error") && (
            <>
              <Button variant="outline" onClick={onClose} disabled={isSubmitting}>
                Cancel
              </Button>
              <Button
                onClick={handleSubmit}
                disabled={isSubmitting}
                aria-busy={isSubmitting}
              >
                {isSubmitting ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" /> Publishing…
                  </>
                ) : step === "error" ? (
                  "Retry"
                ) : (
                  "Publish"
                )}
              </Button>
            </>
          )}
          {step === "success" && (
            <Button onClick={onClose}>Done</Button>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

export default PublishAgentModal;
