"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SourceQuestion, reportSource, type SourceAnswer } from "@/components/onboarding/SourceQuestion";
import { clearWorkspaceDoor, readWorkspaceDoor, touchPayload } from "@/lib/analytics";
import { useNameGate } from "@/hooks/useNameGate";
import { ApiError, useApi } from "@/lib/api";
import { useAgentConfig } from "@/components/voice/data";

/** Mirrors the backend's `company_identity.slugify`: the availability check rejects any
 *  slug this would change, so the two must agree. */
function slugify(value: string): string {
  const trim = (s: string) => s.replace(/^-+|-+$/g, "");
  return trim(trim(value.normalize("NFKD").replace(/[^\x00-\x7f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-")).slice(0, 32));
}

type SlugCheck = { slug: string; available: boolean; reason: "invalid" | "reserved" | "taken" | null; domain: string };

const SLUG_PROBLEM: Record<NonNullable<SlugCheck["reason"]>, string> = {
  invalid: "Use lowercase letters, numbers and hyphens, up to 32 characters.",
  reserved: "That address is reserved. Try another.",
  taken: "That address is taken. Try another.",
};

/** "Set up your business": its name, plus the address its assistant gets Meet invites at. The slug is HELD, then
 *  claimed with the name for the signed-in person's own account; the name also opens the assistant's setup
 *  (the voice document's `business`), so /voice knows a business is saved. No Clerk call. */
function BusinessForm() {
  const api = useApi();
  const router = useRouter();
  const { data: config, error: configError, save } = useAgentConfig();
  // A business is already saved: this form is for naming one, never for renaming it (that would hold a second slug
  // and part the assistant's business name from the account's). Back to /voice before any write.
  const named = !!config?.business?.trim();
  useEffect(() => { if (named) router.replace("/voice"); }, [named, router]);
  const [name, setName] = useState("");
  const [typedSlug, setTypedSlug] = useState<string | null>(null);
  const [check, setCheck] = useState<SlugCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editingSlug, setEditingSlug] = useState(false);
  const [source, setSource] = useState<SourceAnswer>({ answer: "", detail: "" });
  const slug = typedSlug || slugify(name); // clearing the field goes back to the suggestion

  useEffect(() => {
    if (!slug) return;
    let stale = false; // an answer for a slug the user has typed past must not land
    const timer = setTimeout(() => {
      api.get(`/company/slug-availability?slug=${encodeURIComponent(slug)}`)
        .then((r) => { if (!stale) setCheck(r as SlugCheck); })
        .catch(() => { if (!stale) setCheck(null); });
    }, 300);
    return () => { stale = true; clearTimeout(timer); };
  }, [slug, api]);

  const current = check?.slug === slug ? check : null;
  const ready = !!name.trim() && !!current?.available && !!config && !named && !busy;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready || !config) return;
    setBusy(true);
    setError(null);
    const business = name.trim();
    try {
      // Where they came from. Never blocks the setup.
      await api
        .post("/company/source", {
          answer: source.answer || null,
          detail: source.detail || null,
          door: readWorkspaceDoor() ?? "sign_up",
          ...touchPayload(),
        })
        .catch(() => undefined);
      await api.post("/company/slug/hold", { slug });
      // An account that already has its address (a reload after this step) keeps it; any other refusal stops here,
      // before the assistant's document is written. The document's `business` is empty here (a saved one never reaches
      // this form), so nothing stored is overwritten; it must still be written or /voice sends the person back here.
      await api.post("/company/slug/claim", { slug, name: business }).catch((e) => {
        if (!(e instanceof ApiError && e.status === 409 && (e.body as { detail?: string })?.detail === "slug_already_claimed")) throw e;
      });
      await save({ ...config, business, onboardingStep: config.onboardingStep || 1 });
      clearWorkspaceDoor();
      reportSource("sign_up", source);
      router.replace("/voice");
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setCheck({ slug, available: false, reason: "taken", domain: current?.domain ?? "" });
      } else {
        setError("We couldn't set up your business. Please try again.");
      }
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex w-full max-w-md flex-col gap-4 rounded-xl border border-border bg-card p-6 shadow-sm">
      <label className="flex flex-col gap-1.5 text-sm font-medium">
        Business name
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Your salon, spa or barbershop" autoFocus maxLength={64} />
      </label>
      {editingSlug || current?.reason ? (
        <>
          <label className="flex flex-col gap-1.5 text-sm font-medium">
            Meeting invite address
            <Input
              value={slug}
              onChange={(e) => { setEditingSlug(true); setTypedSlug(e.target.value.toLowerCase()); }}
              placeholder="your-business"
              maxLength={32}
              spellCheck={false}
              autoCapitalize="none"
              aria-describedby="slug-status"
            />
          </label>
          <p id="slug-status" className="-mt-2 text-sm text-muted-foreground" aria-live="polite">
            {!slug ? "Your assistant's meeting address comes from this."
              : current?.reason ? <span className="text-destructive">{SLUG_PROBLEM[current.reason]}</span>
              : current ? <>Invite <span className="font-mono text-foreground">agents@{slug}.{current.domain}</span> to a Google Meet and your assistant joins.</>
              : "Checking…"}
          </p>
        </>
      ) : (
        <p id="slug-status" className="-mt-2 flex items-center gap-2 text-sm text-muted-foreground" aria-live="polite">
          {!slug ? "Your assistant's meeting address comes from the name."
            : current ? <span>Invite <span className="font-mono text-foreground">agents@{slug}.{current.domain}</span> to a Google Meet and your assistant joins.</span>
            : "Checking…"}
          {slug && (
            <button type="button" className="text-foreground underline underline-offset-2" onClick={() => setEditingSlug(true)}>
              Change
            </button>
          )}
        </p>
      )}
      <SourceQuestion value={source} onChange={setSource} />
      {configError && <p role="alert" className="text-sm text-destructive">We couldn&apos;t load your setup. Reload the page to try again.</p>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <Button type="submit" disabled={!ready} className="self-end">
        {busy ? "Setting up…" : "Continue"}
      </Button>
    </form>
  );
}

export default function OnboardingPage() {
  // Ask for a name first, so the business is never set up under a nameless account.
  const naming = useNameGate();
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-6 bg-background p-4">
      {naming ? <p role="status">Loading…</p> : (
        <>
          <h1 className="text-2xl font-bold">Set up your business</h1>
          <p>Start with your business name. Then you&apos;ll tell your assistant your hours and services, set up its phone line, and pick its name.</p>
          <BusinessForm />
        </>
      )}
    </main>
  );
}
