"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";

import { useApi } from "@/lib/api";
import { useAgents, agentDisplayName, type Agent } from "@/hooks/useAgents";
import {
  artifactPreview,
  type ArtifactPreview,
  type AvailableSkill,
  type SafetyFlag,
} from "@/lib/marketplace/api";
import type { PublishForm } from "./PublishWizard";

/**
 * Whether the seller has a Stripe Connect payout account on file.
 *
 * Wired to GET /marketplace/payouts/dashboard — that endpoint returns
 * `{ dashboard_url: null }` when no account exists and the seller-hosted
 * login URL otherwise. We treat any non-null URL as "onboarded", matching
 * MyListingsView's own gate.
 */
function usePayoutsOnboarded(): {
  onboarded: boolean | null;
  refresh: () => void;
} {
  const api = useApi();
  const [onboarded, setOnboarded] = useState<boolean | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = (await api.get("/marketplace/payouts/dashboard")) as {
          dashboard_url?: string | null;
        };
        if (alive) setOnboarded(!!r.dashboard_url);
      } catch {
        if (alive) setOnboarded(false);
      }
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce]);
  return { onboarded, refresh: () => setNonce((n) => n + 1) };
}


/**
 * Fetch the artifact preview once per agentId and share it across Bundle +
 * Review steps. Selection state lives in the form; the preview's
 * available_mcps / available_skills / persona_files / safety_flags are
 * a pure read of the agent's workspace so they don't change as the seller
 * toggles checkboxes — fetched once, reused everywhere.
 */
function useArtifactPreview(agentId: string | undefined): {
  preview: ArtifactPreview | null;
  error: string | null;
  isLoading: boolean;
} {
  const { getToken } = useAuth();
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setLoading] = useState(false);

  useEffect(() => {
    if (!agentId) {
      setPreview(null);
      setError(null);
      return;
    }
    let alive = true;
    setLoading(true);
    (async () => {
      const jwt = await getToken();
      if (!jwt) return;
      try {
        const p = await artifactPreview({ jwt, agentId });
        if (alive) setPreview(p);
      } catch (e) {
        if (alive) setError((e as Error).message || "Failed to load preview");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [agentId, getToken]);

  return { preview, error, isLoading };
}

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

  return (
    <div>
      <p className="text-[14px] text-[#5b5b5b] leading-snug mb-4">
        Pick which agent in your workspace you&apos;d like to publish. The next
        step lets you choose which integrations ride along.
      </p>
      <div className="grid grid-cols-2 gap-2.5">
        {agents.map((agent) => {
          const isMain = agent.id === defaultId || agent.identity?.name === "main";
          const isSelected = agent.id === selected;
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
                {isMain && (
                  <div className="text-[11px] text-[#5b5b5b]">core agent · not publishable</div>
                )}
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

// ---------------------------------------------------------------------------
// Bundle step — seller picks which MCPs + skills ride along in the listing.
// Persona files (IDENTITY/SOUL/USER/BOOTSTRAP/MEMORY/HEARTBEAT) ship in
// the tarball unconditionally; safety flags surface in the Review step.
// ---------------------------------------------------------------------------

const BUCKET_LABEL: Record<AvailableSkill["bucket"], string> = {
  bundled: "built-in",
  entries: "configured",
  workspace: "in workspace",
  extra_dirs: "container-wide",
  unknown: "not found",
};

const BUCKET_TONE: Record<AvailableSkill["bucket"], string> = {
  bundled: "bg-[#eef4ee] text-[#4d6b54]",
  entries: "bg-[#f4efe1] text-[#7d5f1a]", // amber — secrets to fill on buyer side
  workspace: "bg-[#eef0f6] text-[#4f458b]",
  extra_dirs: "bg-[#fbe9e7] text-[#8a3a2a]",
  unknown: "bg-[#f3eee5] text-[#5b5b5b]",
};

export function BundleStep({
  form,
  onChange,
}: {
  form: PublishForm;
  onChange: (patch: Partial<PublishForm>) => void;
}) {
  const { preview, error, isLoading } = useArtifactPreview(form.agentId);

  function toggleMcp(name: string) {
    const next = form.bundleMcps.includes(name)
      ? form.bundleMcps.filter((m) => m !== name)
      : [...form.bundleMcps, name];
    onChange({ bundleMcps: next });
  }
  function toggleSkill(name: string) {
    const next = form.bundleSkills.includes(name)
      ? form.bundleSkills.filter((s) => s !== name)
      : [...form.bundleSkills, name];
    onChange({ bundleSkills: next });
  }

  return (
    <div>
      <p className="text-[14px] text-[#5b5b5b] leading-snug mb-4">
        Pick which integrations ride along with your agent. Buyers will need to
        fill in their own credentials for any MCP or configured skill.
      </p>

      {isLoading && <div className="text-[13px] text-[#5b5b5b]">Loading…</div>}
      {error && (
        <div role="alert" className="text-[13px] text-[#b32424] mb-3">
          Couldn&apos;t load preview: {error}
        </div>
      )}

      {preview && (
        <>
          <section className="mb-5" data-testid="bundle-mcps">
            <h4 className="text-[12.5px] font-semibold text-[#1a1a1a] mb-2">
              MCP servers
            </h4>
            {preview.available_mcps.length === 0 ? (
              <p className="text-[12.5px] text-[#5b5b5b]">
                No MCP servers configured on your container.
              </p>
            ) : (
              <ul className="space-y-1.5">
                {preview.available_mcps.map((name) => (
                  <li key={name}>
                    <label className="flex items-center gap-2 cursor-pointer text-[13px]">
                      <input
                        type="checkbox"
                        checked={form.bundleMcps.includes(name)}
                        onChange={() => toggleMcp(name)}
                        aria-label={`bundle MCP ${name}`}
                      />
                      <span className="font-mono text-[12.5px]">{name}</span>
                      <span className="text-[10.5px] px-1.5 py-0.5 rounded-full bg-[#f4efe1] text-[#7d5f1a]">
                        secrets scrubbed
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="mb-5" data-testid="bundle-skills">
            <h4 className="text-[12.5px] font-semibold text-[#1a1a1a] mb-2">
              Skills
            </h4>
            {preview.available_skills.length === 0 ? (
              <p className="text-[12.5px] text-[#5b5b5b]">
                This agent has no skills configured.
              </p>
            ) : (
              <ul className="space-y-1.5">
                {preview.available_skills.map((s) => (
                  <li key={s.name}>
                    <label className="flex items-center gap-2 cursor-pointer text-[13px]">
                      <input
                        type="checkbox"
                        checked={form.bundleSkills.includes(s.name)}
                        onChange={() => toggleSkill(s.name)}
                        aria-label={`bundle skill ${s.name}`}
                      />
                      <span className="font-mono text-[12.5px]">{s.name}</span>
                      <span
                        className={`text-[10.5px] px-1.5 py-0.5 rounded-full ${BUCKET_TONE[s.bucket]}`}
                        title={`bucket: ${s.bucket}`}
                      >
                        {BUCKET_LABEL[s.bucket]}
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {preview.persona_files.length > 0 && (
            <section className="mb-2" data-testid="bundle-persona-files">
              <h4 className="text-[12.5px] font-semibold text-[#1a1a1a] mb-1.5">
                Personality files (always included)
              </h4>
              <p className="text-[11.5px] text-[#5b5b5b] mb-1.5">
                These ship as authored. Review your wording before submitting —
                anything you wrote, your buyer reads.
              </p>
              <div className="flex flex-wrap gap-1.5">
                {preview.persona_files.map((name) => (
                  <span
                    key={name}
                    className="font-mono text-[11px] px-2 py-0.5 rounded-full bg-[#eef4ee] text-[#4d6b54]"
                  >
                    {name}
                  </span>
                ))}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}

function SafetyFlagRow({ flag }: { flag: SafetyFlag }) {
  const tone =
    flag.severity === "high"
      ? "bg-[#fbe9e7] text-[#8a3a2a] border-[#f0b3a8]"
      : flag.severity === "medium"
        ? "bg-[#fff4d6] text-[#7d5f1a] border-[#e8d49a]"
        : "bg-[#f3eee5] text-[#5b5b5b] border-[#e0dbd0]";
  return (
    <li className={`border rounded-lg px-2.5 py-1.5 text-[12px] ${tone}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-[11.5px]">{flag.pattern}</span>
        <span className="font-mono text-[11px] opacity-80">
          {flag.file}
          {flag.line ? `:${flag.line}` : ""}
        </span>
      </div>
      <div className="font-mono text-[11.5px] opacity-90 mt-0.5 break-all">
        {flag.snippet}
      </div>
    </li>
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
  const api = useApi();
  const { onboarded } = usePayoutsOnboarded();
  const needsPayoutSetup = form.priceTier === "paid" && onboarded === false;
  const [payoutBusy, setPayoutBusy] = useState(false);

  async function startPayoutOnboarding() {
    setPayoutBusy(true);
    try {
      const r = (await api.post("/marketplace/payouts/onboard", {})) as {
        onboarding_url: string;
      };
      window.location.href = r.onboarding_url;
    } finally {
      setPayoutBusy(false);
    }
  }

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

      {needsPayoutSetup && (
        <div
          role="alert"
          data-testid="payout-setup-gate"
          className="mb-4 rounded-xl border border-[#e8d49a] bg-[#fff4d6] px-3.5 py-3 text-[13px] text-[#7d5f1a] leading-snug"
        >
          <strong className="block text-[#1a1a1a] mb-0.5">
            Set up payouts first.
          </strong>
          We can&apos;t charge buyers without a Stripe Connect account on file.
          You can finish the rest of the listing first, but submitting at a
          premium tier requires payouts.
          <button
            type="button"
            onClick={startPayoutOnboarding}
            disabled={payoutBusy}
            className="block mt-2 bg-[#06402b] hover:bg-[#0a5a3d] disabled:opacity-60 text-white px-3.5 py-1.5 rounded-full text-[12.5px] font-semibold"
          >
            {payoutBusy ? "Opening Stripe…" : "Set up payouts →"}
          </button>
        </div>
      )}

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
  const { preview } = useArtifactPreview(form.agentId);
  const mcpCount = form.bundleMcps.length;
  const skillCount = form.bundleSkills.length;
  const personaCount = preview?.persona_files.length ?? 0;
  // Show warning only if the selection actually adds something to the
  // buyer's openclaw.json (an MCP, or a skill in a config-keyed bucket).
  const needsConfigPatch =
    mcpCount > 0 ||
    (preview != null &&
      form.bundleSkills.some((name) => {
        const s = preview.available_skills.find((x) => x.name === name);
        return s?.bucket === "entries" || s?.bucket === "bundled";
      }));
  const safetyFlags = preview?.safety_flags ?? [];

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
          <span data-testid="review-bundle-summary">
            Bundled: {mcpCount} MCP{mcpCount === 1 ? "" : "s"} · {skillCount} skill
            {skillCount === 1 ? "" : "s"} · {personaCount} persona file
            {personaCount === 1 ? "" : "s"}
          </span>
          <span className="font-semibold text-[#06402b]">
            {form.priceTier === "free" ? "Free" : "Premium tier"}
          </span>
        </div>
      </div>

      {needsConfigPatch && (
        <div
          role="status"
          data-testid="review-config-patch-warning"
          className="mt-3 rounded-xl border border-[#e8d49a] bg-[#fff4d6] px-3.5 py-2.5 text-[12.5px] text-[#7d5f1a] leading-snug"
        >
          Heads up — installing this listing will patch your buyer&apos;s
          openclaw.json (MCPs / configured skills). Buyers will need to fill in
          their own credentials before those integrations work.
        </div>
      )}

      {safetyFlags.length > 0 && (
        <section
          className="mt-3"
          aria-label="Safety scan findings"
          data-testid="review-safety-flags"
        >
          <h4 className="text-[12.5px] font-semibold text-[#1a1a1a] mb-1.5">
            Safety scan: {safetyFlags.length} finding{safetyFlags.length === 1 ? "" : "s"}
          </h4>
          <p className="text-[11.5px] text-[#5b5b5b] mb-2">
            Found these patterns in your listing files. Trust check before submit
            — edit the source, then come back to re-snapshot.
          </p>
          <ul className="space-y-1.5">
            {safetyFlags.map((f, i) => (
              <SafetyFlagRow flag={f} key={`${f.file}-${f.line ?? 0}-${i}`} />
            ))}
          </ul>
        </section>
      )}

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

export function SuccessStep({
  onViewMine,
  mode = "submit",
}: {
  onViewMine: () => void;
  mode?: "submit" | "resubmit";
}) {
  return (
    <div className="text-center py-8">
      <div className="text-3xl mb-3">✓</div>
      <h3 className="font-editorial-serif text-2xl mb-2">
        {mode === "resubmit" ? "Resubmitted for review." : "Submitted for review."}
      </h3>
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
