"use client";

import { useAuth } from "@clerk/nextjs";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";

import { Monogram, AgentPill } from "@/components/agents/AgentIdentity";
import { fromListing, initialsFor, paletteFor } from "@/lib/agentDesign";
import { SITE_URL } from "@/lib/domain";
import { getListing } from "@/lib/marketplace/api";
import type { ListingDetailResponse } from "@/lib/marketplace/types";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";
const APP_URL = SITE_URL;

function titleCase(token: string): string {
  return token
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(" ");
}

export default function DeploySuccessPage() {
  return (
    <Suspense fallback={<Loading />}>
      <DeploySuccessInner />
    </Suspense>
  );
}

function Loading() {
  return (
    <main className="mx-auto flex min-h-[60vh] max-w-md items-center justify-center px-6 py-16">
      <p className="font-editorial-serif text-2xl italic text-[var(--midnight-muted)]">Loading…</p>
    </main>
  );
}

function DeploySuccessInner() {
  const params = useSearchParams();
  const listingId = params.get("listing_id") ?? "";
  const sessionId = params.get("session_id");
  // entitled=1: the buyer already owns this listing (BuyButton got 409
  // already_entitled from checkout) — run the same on-mount deploy as a paid
  // return, just without a Stripe session id.
  const entitled = params.get("entitled") === "1";
  const initialAgentUuid = params.get("agent_uuid");

  const { isSignedIn, getToken, isLoaded } = useAuth();

  const [agentUuid, setAgentUuid] = useState<string | null>(initialAgentUuid);
  const [phase, setPhase] = useState<
    "idle" | "deploying" | "ready" | "entitlement_pending" | "in_progress" | "error"
  >(initialAgentUuid ? "ready" : "idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  // ?pending= is set by BuyButton, which ran the deploy itself — on that path
  // this page never sees the response, so the count has to ride the redirect.
  const [automationsPending, setAutomationsPending] = useState(Number(params.get("pending")) || 0);
  const [errorRetryable, setErrorRetryable] = useState(true);
  const [detail, setDetail] = useState<ListingDetailResponse | null>(null);

  // Editorial identity — uses the fetched listing if available, otherwise
  // derives a placeholder from the listing_id so the celebration still has
  // shape even before the listing fetch completes.
  const editorial = useMemo(() => {
    if (detail) {
      const fromList = fromListing(detail.listing);
      const vibe = detail.manifest?.vibe || fromList.vibe;
      return { ...fromList, vibe };
    }
    const palette = paletteFor(listingId || "agent");
    return {
      slug: listingId,
      name: titleCase(listingId) || "Your agent",
      role: "Agent",
      monogram: initialsFor(listingId.replace(/[-_]/g, " ") || "Agent"),
      accent: palette.accent,
      tint: palette.tint,
      vibe: "Settling into your container.",
      isFree: false,
      price: "",
      skills: [] as string[],
      tags: [] as string[],
    };
  }, [detail, listingId]);

  // Send unauthed users to sign-in.
  useEffect(() => {
    if (isLoaded && !isSignedIn) {
      window.location.href = `/sign-in?redirect_url=${encodeURIComponent(window.location.href)}`;
    }
  }, [isLoaded, isSignedIn]);

  // Fetch listing details for the celebration. Best-effort; if it fails, we
  // fall back to listing_id-derived identity.
  useEffect(() => {
    if (!listingId) return;
    let cancelled = false;
    (async () => {
      try {
        const result = await getListing(listingId);
        if (cancelled) return;
        setDetail(result);
      } catch {
        // Fall back to listing_id-derived identity.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [listingId]);

  // Paid-return / already-entitled path: fire deploy on mount once Clerk
  // auth is loaded.
  useEffect(() => {
    if (!isLoaded || !isSignedIn) return;
    if (initialAgentUuid) return;
    if (!sessionId && !entitled) return;
    if (!listingId) return;
    if (phase !== "idle") return;

    let cancelled = false;
    (async () => {
      setPhase("deploying");
      try {
        const jwt = await getToken();
        const resp = await fetch(
          `${API}/marketplace/listings/${encodeURIComponent(listingId)}/deploy`,
          {
            method: "POST",
            headers: {
              "content-type": "application/json",
              Authorization: `Bearer ${jwt}`,
            },
          },
        );
        if (cancelled) return;
        if (resp.status === 403) {
          setPhase("entitlement_pending");
          return;
        }
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          const d = body?.detail;
          // Structured 409s: deploy_in_progress means THIS deploy is already
          // running (a mid-deploy refresh lands here) — "Try again" is the
          // exact retry-invitation that once duplicated an agent, so it gets
          // its own no-retry card. Rendering the raw object printed
          // "[object Object]".
          if (d && typeof d === "object") {
            if (d.error === "deploy_in_progress") {
              setPhase("in_progress");
              return;
            }
            if (d.error === "missing_credentials") {
              const missing = (d.missing ?? []).join(", ");
              setErrorRetryable(false);
              throw new Error(
                missing
                  ? `Connect ${missing} in Settings → Integrations first, then come back to finish hiring.`
                  : "Connect the required integrations in Settings first, then come back to finish hiring.",
              );
            }
            throw new Error(d.error ?? `deploy failed (${resp.status})`);
          }
          throw new Error(typeof d === "string" ? d : `deploy failed (${resp.status})`);
        }
        const body = (await resp.json()) as {
          agent_uuid?: string;
          automations_pending?: unknown[];
        };
        if (body.agent_uuid) setAgentUuid(body.agent_uuid);
        // Webhook automations the deploy deliberately did NOT arm — each binds
        // an account only the buyer can connect. Say so, or the agent looks
        // complete and quietly never fires.
        setAutomationsPending(body.automations_pending?.length ?? 0);
        setPhase("ready");
      } catch (e) {
        if (cancelled) return;
        setErrorMsg(e instanceof Error ? e.message : "Deploy failed.");
        setPhase("error");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isLoaded, isSignedIn, getToken, listingId, sessionId, entitled, initialAgentUuid, phase]);

  if (isLoaded && !isSignedIn) return <Loading />;

  return (
    <main className="mx-auto max-w-xl px-6 py-20">
      {phase === "deploying" && (
        <div className="text-center">
          <div className="mb-6 flex justify-center">
            <Monogram agent={editorial} size={96} radius={20} />
          </div>
          <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--midnight)]">
            Hiring <em className="text-[var(--midnight)]">{editorial.name}</em>…
          </h1>
          <p className="mt-4 font-editorial-serif text-lg italic text-[var(--midnight-muted)]">
            Setting up the workspace.
          </p>
        </div>
      )}

      {phase === "entitlement_pending" && (
        <div className="editorial-card p-8 text-center">
          <div className="mb-5 flex justify-center">
            <Monogram agent={editorial} size={72} radius={16} />
          </div>
          <h1 className="font-editorial-serif text-4xl italic text-[var(--midnight)]">Almost there.</h1>
          <p className="mt-4 text-base leading-7 text-[var(--midnight-soft)]">
            Stripe&apos;s confirmation webhook is taking a moment. Refresh in a few seconds and
            we&apos;ll finish hiring {editorial.name}.
          </p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="editorial-btn editorial-btn--primary mt-6 h-11 px-6"
          >
            Refresh
          </button>
        </div>
      )}

      {phase === "in_progress" && (
        <div className="editorial-card p-8 text-center">
          <div className="mb-5 flex justify-center">
            <Monogram agent={editorial} size={72} radius={16} />
          </div>
          <h1 className="font-editorial-serif text-4xl italic text-[var(--midnight)]">
            Already on it.
          </h1>
          <p className="mt-4 text-base leading-7 text-[var(--midnight-soft)]">
            {editorial.name} is already installing — give it a moment, then open your workspace.
          </p>
          <Link
            href={`${APP_URL}/chat`}
            className="editorial-btn editorial-btn--primary mt-6 inline-flex h-11 px-6"
          >
            Open workspace
          </Link>
        </div>
      )}

      {phase === "error" && (
        <div className="editorial-card p-8 text-center">
          <h1 className="font-editorial-serif text-4xl italic text-[var(--midnight)]">
            Something went sideways.
          </h1>
          <p className="mt-4 rounded-lg border border-[var(--err)] px-4 py-3 text-sm text-[var(--err)]">
            {errorMsg}
          </p>
          {errorRetryable && (
            <button
              type="button"
              onClick={() => {
                setErrorMsg(null);
                setPhase("idle");
              }}
              className="editorial-btn editorial-btn--primary mt-6 h-11 px-6"
            >
              Try again
            </button>
          )}
        </div>
      )}

      {phase === "ready" && (
        <div className="text-center">
          <div className="mb-6 flex justify-center">
            <Monogram agent={editorial} size={96} radius={20} />
          </div>
          <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--midnight)]">
            You hired <em className="text-[var(--midnight)]">{editorial.name}.</em>
          </h1>
          <div className="mt-4 flex items-center justify-center gap-2">
            <AgentPill>{editorial.role}</AgentPill>
            <AgentPill tone="midnight">Active in your container</AgentPill>
          </div>
          <p className="mx-auto mt-6 max-w-md font-editorial-serif text-lg italic leading-8 text-[var(--midnight-soft)]">
            &ldquo;{editorial.vibe}&rdquo;
          </p>
          <p className="mt-3 text-sm leading-6 text-[var(--midnight-muted)]">
            {editorial.name} is settling in. Open chat to introduce yourself.
          </p>
          {automationsPending > 0 && (
            <p
              data-testid="deploy-automations-pending"
              className="mx-auto mt-3 max-w-md text-sm leading-6 text-[var(--midnight-muted)]"
            >
              {automationsPending === 1
                ? "1 automation needs wiring"
                : `${automationsPending} automations need wiring`}{" "}
              — each one listens to an account only you can connect. Finish setting them up in
              Automations.
            </p>
          )}
          <Link
            href={
              agentUuid
                ? `${APP_URL}/chat?agent=${encodeURIComponent(agentUuid)}`
                : `${APP_URL}/chat`
            }
            className="editorial-btn editorial-btn--primary mt-8 inline-flex h-12 px-8 text-sm"
          >
            Open chat with {editorial.name} →
          </Link>
          {agentUuid && (
            <p className="mt-6 font-mono text-[11px] uppercase tracking-[0.14em] text-[var(--midnight-muted)]">
              Agent ID · {agentUuid}
            </p>
          )}
        </div>
      )}

      {phase === "idle" && <Loading />}
    </main>
  );
}
