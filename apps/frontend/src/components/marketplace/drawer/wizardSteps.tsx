"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";

import { useAgents, agentDisplayName, type Agent } from "@/hooks/useAgents";
import { myAgentsSummary, type AgentSummary } from "@/lib/marketplace/api";
import type { PublishForm } from "./PublishWizard";

export function PickAgentStep({
  selected,
  onSelect,
}: {
  selected?: string;
  onSelect: (id: string) => void;
}) {
  const { agents, defaultId } = useAgents() as unknown as {
    agents: Agent[];
    defaultId?: string;
  };
  const { getToken } = useAuth();
  const [summary, setSummary] = useState<AgentSummary[] | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      const jwt = await getToken();
      if (!jwt) return;
      const r = await myAgentsSummary({ jwt });
      if (alive) setSummary(r.agents);
    })().catch(() => {
      if (alive) setSummary([]);
    });
    return () => {
      alive = false;
    };
  }, [getToken]);

  const summaryById: Record<string, AgentSummary> = (summary ?? []).reduce(
    (acc, a) => ({ ...acc, [a.agent_id]: a }),
    {},
  );

  return (
    <div>
      <p className="text-[14px] text-[#5b5b5b] leading-snug mb-4">
        Pick which agent in your workspace you&apos;d like to publish. We&apos;ll bundle its config,
        skills, and cron jobs into a deployable artifact.
      </p>
      <div className="grid grid-cols-2 gap-2.5">
        {agents.map((agent) => {
          const isMain = agent.id === defaultId || agent.identity?.name === "main";
          const isSelected = agent.id === selected;
          const sub = summaryById[agent.id];
          return (
            <div
              key={agent.id}
              role="button"
              aria-disabled={isMain}
              tabIndex={isMain ? -1 : 0}
              onClick={() => !isMain && onSelect(agent.id)}
              className={`flex items-center gap-3 p-3 rounded-2xl border bg-white cursor-pointer transition ${
                isMain
                  ? "opacity-45 cursor-not-allowed bg-[#f3eee5]"
                  : isSelected
                    ? "border-[#06402b] bg-[#f4f8f4] shadow-[0_0_0_3px_rgba(6,64,43,0.08)]"
                    : "border-[#e0dbd0] hover:border-[#cfc7b8] hover:-translate-y-px"
              }`}
              title={isMain ? "main agent can't be published" : undefined}
            >
              <div className="w-9 h-9 rounded-[10px] bg-[#dad4f0] text-[#4f458b] font-editorial-serif font-semibold text-sm flex items-center justify-center">
                {(agent.identity?.name ?? agent.name ?? "?").slice(0, 2)}
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-[14px] font-medium">{agentDisplayName(agent)}</div>
                <div className="text-[11px] text-[#5b5b5b]">
                  {isMain
                    ? "core agent · not publishable"
                    : sub
                      ? `${sub.skill_count} skill${sub.skill_count === 1 ? "" : "s"} · ${sub.cron_count} cron${sub.cron_count === 1 ? "" : "s"}`
                      : "…"}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function DetailsStep({
  form,
  onChange,
}: {
  form: PublishForm;
  onChange: (patch: Partial<PublishForm>) => void;
}) {
  const [tagInput, setTagInput] = useState("");

  function addTag(raw: string) {
    const t = raw.trim().toLowerCase();
    if (!t || form.tags.includes(t) || form.tags.length >= 5) return;
    onChange({ tags: [...form.tags, t] });
  }

  return (
    <div>
      <p className="text-[14px] text-[#5b5b5b] leading-snug mb-4">
        Help buyers find your agent. This is what they&apos;ll see in the marketplace.
      </p>
      <Field label="Public name">
        <input
          value={form.name}
          onChange={(e) => onChange({ name: e.target.value })}
          className="border border-[#e0dbd0] bg-white rounded-xl px-3.5 py-2.5 text-sm w-full focus:border-[#06402b] outline-0"
        />
      </Field>
      <Field label="One-line tagline" hint="Max 80 characters.">
        <input
          maxLength={80}
          value={form.tagline}
          onChange={(e) => onChange({ tagline: e.target.value })}
          className="border border-[#e0dbd0] bg-white rounded-xl px-3.5 py-2.5 text-sm w-full focus:border-[#06402b] outline-0"
        />
      </Field>
      <Field label="Description" hint="Markdown allowed.">
        <textarea
          rows={4}
          value={form.description}
          onChange={(e) => onChange({ description: e.target.value })}
          className="border border-[#e0dbd0] bg-white rounded-xl px-3.5 py-2.5 text-sm w-full focus:border-[#06402b] outline-0 min-h-[88px] resize-y"
        />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Category">
          <select
            value={form.category}
            onChange={(e) => onChange({ category: e.target.value })}
            className="border border-[#e0dbd0] bg-white rounded-xl px-3.5 py-2.5 text-sm w-full focus:border-[#06402b] outline-0"
          >
            {["Operations", "Sales", "Engineering", "Research", "Finance", "Marketing", "Personal"].map(
              (c) => (
                <option key={c}>{c}</option>
              ),
            )}
          </select>
        </Field>
        <Field label="Tags (max 5)">
          <div className="flex gap-1.5 flex-wrap items-center p-2 bg-white border border-[#e0dbd0] rounded-xl min-h-[44px]">
            {form.tags.map((t) => (
              <span
                key={t}
                className="bg-[#f3eee5] text-[#1a1a1a] text-[12px] px-2.5 py-1 rounded-full inline-flex items-center gap-1.5"
              >
                {t}
                <button
                  type="button"
                  onClick={() => onChange({ tags: form.tags.filter((x) => x !== t) })}
                  className="text-[#5b5b5b] hover:text-[#1a1a1a]"
                >
                  ×
                </button>
              </span>
            ))}
            <input
              value={tagInput}
              onChange={(e) => setTagInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addTag(tagInput);
                  setTagInput("");
                } else if (e.key === "Backspace" && !tagInput && form.tags.length) {
                  onChange({ tags: form.tags.slice(0, -1) });
                }
              }}
              placeholder="type + Enter"
              className="flex-1 min-w-[80px] outline-0 bg-transparent text-sm"
            />
          </div>
        </Field>
      </div>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5 mb-3.5">
      <label className="text-[12.5px] font-semibold text-[#1a1a1a]">{label}</label>
      {children}
      {hint && <span className="text-[11.5px] text-[#5b5b5b]">{hint}</span>}
    </div>
  );
}

export function PricingStep({
  form,
  onChange,
}: {
  form: PublishForm;
  onChange: (patch: Partial<PublishForm>) => void;
}) {
  const v = form.priceDollars;
  const stripe = +(v * 0.029 + 0.3).toFixed(2);
  const platform = +(v * 0.15).toFixed(2);
  const take = +(v - stripe - platform).toFixed(2);

  return (
    <div>
      <p className="text-[14px] text-[#5b5b5b] leading-snug mb-4">
        Set what your work is worth. Price is hidden from the marketplace browse view —
        buyers only see it at install time.
      </p>

      <div className="bg-[#f3eee5] rounded-2xl p-1 flex mb-4">
        <button
          type="button"
          onClick={() => onChange({ priceTier: "free" })}
          className={`flex-1 px-3.5 py-2.5 rounded-xl text-sm ${
            form.priceTier === "free"
              ? "bg-white text-[#1a1a1a] shadow-sm font-semibold"
              : "text-[#5b5b5b]"
          }`}
        >
          Free
        </button>
        <button
          type="button"
          onClick={() => onChange({ priceTier: "paid" })}
          className={`flex-1 px-3.5 py-2.5 rounded-xl text-sm ${
            form.priceTier === "paid"
              ? "bg-white text-[#1a1a1a] shadow-sm font-semibold"
              : "text-[#5b5b5b]"
          }`}
        >
          Premium tier
        </button>
      </div>

      {form.priceTier === "free" ? (
        <div className="bg-white border border-[#e0dbd0] rounded-xl p-4 text-[#5b5b5b] text-[13.5px] leading-snug">
          <strong className="text-[#1a1a1a]">Free agents install instantly.</strong>
          <br />
          No payout setup needed. You can switch this listing to a premium tier anytime.
        </div>
      ) : (
        <>
          <label className="block text-[12.5px] font-semibold text-[#1a1a1a] mb-1.5">
            Price per install (USD)
          </label>
          <div className="flex items-center bg-white border border-[#e0dbd0] rounded-xl px-3.5 py-3 gap-2 mb-2">
            <span className="font-editorial-serif text-2xl text-[#5b5b5b]">$</span>
            <input
              type="number"
              min={1}
              max={20}
              step={1}
              value={v}
              onChange={(e) =>
                onChange({ priceDollars: Math.max(1, Math.min(20, +e.target.value || 0)) })
              }
              className="font-editorial-serif text-2xl flex-1 outline-0 bg-transparent"
            />
            <span className="text-[12px] text-[#5b5b5b]">USD · one-time, perpetual license</span>
          </div>
          <p className="text-[11.5px] text-[#5b5b5b] mb-3">
            Suggested range: $5–$20. Listings over $20 require Isol8 approval.
          </p>
          <div className="bg-white border border-[#e0dbd0] rounded-xl px-4 py-3.5 text-[13px]">
            <Row label="List price" value={`$${v.toFixed(2)}`} />
            <Row label="Stripe processing fee (2.9% + $0.30)" value={`−$${stripe.toFixed(2)}`} />
            <Row label="Isol8 platform fee (15%)" value={`−$${platform.toFixed(2)}`} />
            <div className="flex justify-between border-t border-[#e0dbd0] mt-1.5 pt-2.5 text-[#1a1a1a] font-semibold">
              <span>Your take-home per install</span>
              <span className="font-editorial-serif text-[#06402b] text-base">
                ${take.toFixed(2)}
              </span>
            </div>
          </div>
          <p className="text-[12px] italic text-[#5b5b5b] mt-3">
            Earnings accrue to your seller balance. Cash out anytime from My Listings.
          </p>
        </>
      )}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between text-[#5b5b5b] py-0.5">
      <span>{label}</span>
      <span>{value}</span>
    </div>
  );
}

export function ReviewStep({ form }: { form: PublishForm }) {
  const { getToken } = useAuth();
  const [summary, setSummary] = useState<AgentSummary | null>(null);

  useEffect(() => {
    if (!form.agentId) return;
    let alive = true;
    (async () => {
      const jwt = await getToken();
      if (!jwt) return;
      const r = await myAgentsSummary({ jwt });
      if (!alive) return;
      const match = r.agents.find((a) => a.agent_id === form.agentId);
      if (match) setSummary(match);
    })().catch(() => {
      // best effort — leave summary null
    });
    return () => {
      alive = false;
    };
  }, [form.agentId, getToken]);

  return (
    <div>
      <p className="text-[14px] text-[#5b5b5b] leading-snug mb-4">
        Here&apos;s how your listing will appear. Submit when ready — listings go live after
        admin moderation (usually within 24h).
      </p>
      <div className="bg-white border border-[#e0dbd0] rounded-2xl p-5">
        <div className="font-editorial-serif text-xl">{form.name || "Untitled"}</div>
        <div className="text-[12px] text-[#5b5b5b] italic mt-0.5">by you</div>
        <p className="text-[13.5px] text-[#5b5b5b] leading-snug mt-2">{form.tagline || "—"}</p>
        {form.tags.length > 0 && (
          <div className="flex gap-1.5 flex-wrap mt-3">
            {form.tags.map((t) => (
              <span
                key={t}
                className="bg-[#f3eee5] text-[#5b5b5b] text-[11px] px-2 py-0.5 rounded-full"
              >
                {t}
              </span>
            ))}
          </div>
        )}
        <div className="flex justify-between text-[12.5px] text-[#5b5b5b] mt-3 pt-3 border-t border-dashed border-[#e0dbd0]">
          <span>
            {summary
              ? `Bundled: ${summary.skill_count} skill${summary.skill_count === 1 ? "" : "s"} · ${summary.cron_count} cron${summary.cron_count === 1 ? "" : "s"} · ${summary.channel_count} channel${summary.channel_count === 1 ? "" : "s"}`
              : "Bundled: …"}
          </span>
          <span className="font-semibold text-[#06402b]">
            {form.priceTier === "free" ? "Free" : "Premium tier"}
          </span>
        </div>
      </div>
      <p className="text-[12px] text-[#5b5b5b] leading-snug mt-3">
        By publishing, you agree to the{" "}
        <a href="#" className="text-[#06402b] underline">
          Marketplace Terms
        </a>{" "}
        and confirm this agent is yours to publish. Isol8 retains the right to remove listings
        that violate the{" "}
        <a href="#" className="text-[#06402b] underline">
          Acceptable Use Policy
        </a>
        .
      </p>
    </div>
  );
}

export function SuccessStep({ onViewMine }: { onViewMine: () => void }) {
  return (
    <div className="text-center py-8">
      <div className="text-3xl mb-3">✓</div>
      <h3 className="font-editorial-serif text-2xl mb-2">Submitted for review.</h3>
      <p className="text-[14px] text-[#5b5b5b] mb-4">
        You&apos;ll get a notification when it&apos;s live — usually within 24 hours.
      </p>
      <button
        onClick={onViewMine}
        className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-5 py-2 rounded-full font-semibold"
      >
        View my listings →
      </button>
    </div>
  );
}
