"use client";

import { useAuth } from "@clerk/nextjs";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";

import { Monogram, AgentPill } from "@/components/agents/AgentIdentity";
import { fromListing, initialsFor, paletteFor } from "@/lib/agentDesign";
import { getListing } from "@/lib/marketplace/api";
import type { ListingDetailResponse } from "@/lib/marketplace/types";

const API = process.env.NEXT_PUBLIC_API_URL ?? "https://api.isol8.co";
const APP_URL = process.env.NEXT_PUBLIC_APP_URL ?? "https://app.isol8.co";

function titleCase(slug: string): string {
  return slug
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
      <p className="font-editorial-serif text-2xl italic text-[var(--mute)]">Loading…</p>
    </main>
  );
}

function DeploySuccessInner() {
  const params = useSearchParams();
  const listingSlug = params.get("listing_slug") ?? "";
  const sessionId = params.get("session_id");
  const initialAgentUuid = params.get("agent_uuid");

  const { isSignedIn, getToken, isLoaded } = useAuth();

  const [agentUuid, setAgentUuid] = useState<string | null>(initialAgentUuid);
  const [phase, setPhase] = useState<
    "idle" | "deploying" | "ready" | "entitlement_pending" | "error"
  >(initialAgentUuid ? "ready" : "idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [detail, setDetail] = useState<ListingDetailResponse | null>(null);

  // Editorial identity — uses the fetched listing if available, otherwise
  // derives a placeholder from the slug so the celebration still has shape
  // even before the listing fetch completes.
  const editorial = useMemo(() => {
    if (detail) {
      const fromList = fromListing(detail.listing);
      const vibe = detail.manifest?.vibe || fromList.vibe;
      return { ...fromList, vibe };
    }
    const palette = paletteFor(listingSlug || "agent");
    return {
      slug: listingSlug,
      name: titleCase(listingSlug) || "Your agent",
      role: "Agent",
      monogram: initialsFor(listingSlug.replace(/[-_]/g, " ") || "Agent"),
      accent: palette.accent,
      tint: palette.tint,
      vibe: "Settling into your container.",
      isFree: false,
      price: "",
      skills: [] as string[],
      tags: [] as string[],
    };
  }, [detail, listingSlug]);

  // Send unauthed users to sign-in.
  useEffect(() => {
    if (isLoaded && !isSignedIn) {
      window.location.href = `/sign-in?redirect_url=${encodeURIComponent(window.location.href)}`;
    }
  }, [isLoaded, isSignedIn]);

  // Fetch listing details for the celebration. Best-effort; if it fails, we
  // fall back to slug-derived identity.
  useEffect(() => {
    if (!listingSlug) return;
    let cancelled = false;
    (async () => {
      try {
        const result = await getListing(listingSlug);
        if (cancelled) return;
        setDetail(result);
      } catch {
        // Fall back to slug-derived identity.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [listingSlug]);

  // Paid-return path: fire deploy on mount once Clerk auth is loaded.
  useEffect(() => {
    if (!isLoaded || !isSignedIn) return;
    if (initialAgentUuid) return;
    if (!sessionId) return;
    if (!listingSlug) return;
    if (phase !== "idle") return;

    let cancelled = false;
    (async () => {
      setPhase("deploying");
      try {
        const jwt = await getToken();
        const resp = await fetch(
          `${API}/api/v1/marketplace/listings/${encodeURIComponent(listingSlug)}/deploy`,
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
          throw new Error(body?.detail ?? `deploy failed (${resp.status})`);
        }
        const body = (await resp.json()) as { agent_uuid?: string };
        if (body.agent_uuid) setAgentUuid(body.agent_uuid);
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
  }, [isLoaded, isSignedIn, getToken, listingSlug, sessionId, initialAgentUuid, phase]);

  if (isLoaded && !isSignedIn) return <Loading />;

  return (
    <main className="mx-auto max-w-xl px-6 py-20">
      {phase === "deploying" && (
        <div className="text-center">
          <div className="mb-6 flex justify-center">
            <Monogram agent={editorial} size={96} radius={20} />
          </div>
          <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--ink)]">
            Hiring <em className="text-[var(--forest)]">{editorial.name}</em>…
          </h1>
          <p className="mt-4 font-editorial-serif text-lg italic text-[var(--mute)]">
            Setting up the workspace.
          </p>
        </div>
      )}

      {phase === "entitlement_pending" && (
        <div className="editorial-card p-8 text-center">
          <div className="mb-5 flex justify-center">
            <Monogram agent={editorial} size={72} radius={16} />
          </div>
          <h1 className="font-editorial-serif text-4xl italic text-[var(--ink)]">Almost there.</h1>
          <p className="mt-4 text-base leading-7 text-[var(--ink-soft)]">
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

      {phase === "error" && (
        <div className="editorial-card p-8 text-center">
          <h1 className="font-editorial-serif text-4xl italic text-[var(--ink)]">
            Something went sideways.
          </h1>
          <p className="mt-4 rounded-lg border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] px-4 py-3 text-sm text-[var(--danger)]">
            {errorMsg}
          </p>
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
        </div>
      )}

      {phase === "ready" && (
        <div className="text-center">
          <div className="mb-6 flex justify-center">
            <Monogram agent={editorial} size={96} radius={20} />
          </div>
          <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--ink)]">
            You hired <em className="text-[var(--forest)]">{editorial.name}.</em>
          </h1>
          <div className="mt-4 flex items-center justify-center gap-2">
            <AgentPill>{editorial.role}</AgentPill>
            <AgentPill tone="forest">Active in your container</AgentPill>
          </div>
          <p className="mx-auto mt-6 max-w-md font-editorial-serif text-lg italic leading-8 text-[var(--ink-soft)]">
            &ldquo;{editorial.vibe}&rdquo;
          </p>
          <p className="mt-3 text-sm leading-6 text-[var(--mute)]">
            {editorial.name} is settling in. Open chat to introduce yourself.
          </p>
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
            <p className="mt-6 font-mono text-[11px] uppercase tracking-[0.14em] text-[var(--mute-soft)]">
              Agent ID · {agentUuid}
            </p>
          )}
        </div>
      )}

      {phase === "idle" && <Loading />}
    </main>
  );
}
