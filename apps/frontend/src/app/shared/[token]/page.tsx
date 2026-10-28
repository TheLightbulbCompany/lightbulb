"use client";

import { useParams } from "next/navigation";
import useSWR from "swr";

import { BACKEND_URL } from "@/lib/api";

interface Shared {
  session: {
    harness: string;
    computer: string;
    repo: string;
    branch: string;
    started: string;
    ended: string | null;
    prompt_tokens: number;
    completion_tokens: number;
  };
  tool_calls: { seq: number; at: string; tool: string; summary?: string | null; ok: boolean }[];
}

async function load(token: string): Promise<Shared | null> {
  const r = await fetch(`${BACKEND_URL}/shared/${encodeURIComponent(token)}`);
  if (r.status === 404) return null;
  if (!r.ok) throw new Error("failed");
  return r.json();
}

export default function SharedSessionPage() {
  const { token } = useParams<{ token: string }>();
  const { data, error, isLoading } = useSWR(["shared", token], () => load(token), { shouldRetryOnError: false });

  if (isLoading) return <main className="mx-auto max-w-2xl p-6 text-sm">Loading…</main>;
  if (error) return <main className="mx-auto max-w-2xl p-6 text-sm">This session could not be loaded.</main>;
  if (!data) return <main className="mx-auto max-w-2xl p-6 text-sm">This link has expired or the session was deleted.</main>;
  const s = data.session;
  return (
    <main className="mx-auto max-w-2xl p-6">
      <h1 className="text-lg font-semibold">
        {s.harness} · {s.repo || "no repo"}
        {s.branch ? ` · ${s.branch}` : ""}
      </h1>
      <p className="mt-1 text-sm text-[var(--fg-mute)]">
        {s.computer ? `${s.computer} · ` : ""}
        {new Date(s.started).toLocaleString()} · {s.prompt_tokens.toLocaleString()} prompt tokens ·{" "}
        {s.completion_tokens.toLocaleString()} completion tokens
      </p>
      {data.tool_calls.length ? (
        <ol className="mt-4">
          {data.tool_calls.map((c) => (
            <li key={c.seq} className="border-b py-2 text-sm">
              <span className="font-medium">{c.tool}</span>
              {c.ok ? "" : " (failed)"}
              {c.summary ? <p className="whitespace-pre-wrap break-words">{c.summary}</p> : null}
            </li>
          ))}
        </ol>
      ) : (
        <p className="mt-4 text-sm text-[var(--fg-soft)]">Only the session&apos;s details were shared, not its tool calls.</p>
      )}
    </main>
  );
}
