"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";

import { useApi } from "@/lib/api";
import { openAuthUrl } from "@/lib/externalAuth";
import { useAgents, agentDisplayName, type Agent } from "@/hooks/useAgents";
import {
  artifactPreview,
  type ArtifactPreview,
  type AvailableSkill,
  type SafetyFlag,
} from "@/lib/marketplace/api";
import type { PublishForm } from "./PublishWizard";
import { ImportBackupDialog } from "./ImportBackupDialog";
import { notifyApiError } from "@/lib/notify";

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
  onSelect: (id: string, name?: string) => void;
}) {
  const { agents, defaultId, refresh } = useAgents() as unknown as {
    agents: Agent[];
    defaultId?: string;
    refresh: () => void;
  };
  const [importOpen, setImportOpen] = useState(false);

  return (
    <div>
      <p className="text-sm text-muted-foreground leading-snug mb-4">
        Pick which agent in your workspace you&apos;d like to publish. The next
        step lets you choose which integrations ride along.
      </p>
      <div className="mb-4 flex items-center justify-between rounded-xl border border-[var(--edge)] bg-card px-3 py-2 text-ui">
        <span className="text-muted-foreground">Been working in Claude Code?</span>
        <button type="button" onClick={() => setImportOpen(true)} className="rounded-full border border-[var(--edge)] px-3 py-1 font-medium text-[var(--midnight)] hover:bg-[var(--midnight)]/5">
          Import your Claude Code context
        </button>
      </div>
      <ImportBackupDialog open={importOpen} onOpenChange={setImportOpen} onImported={(id) => { setImportOpen(false); refresh(); onSelect(id); }} />
      {agents.length === 0 && (
        <div className="rounded-xl border border-dashed border-[var(--edge)] bg-card p-6 text-center">
          <p className="text-sm font-medium text-[var(--midnight)]">No agents to publish yet.</p>
          <p className="mt-1.5 text-ui leading-snug text-muted-foreground">
            If your workspace is still starting, give it a minute and reopen this step. Otherwise,
            build an agent first — then come back to put it on the marketplace.
          </p>
        </div>
      )}
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
              onClick={() => !isMain && onSelect(agent.id, agentDisplayName(agent))}
              onKeyDown={(e) => {
                if (isMain || (e.key !== "Enter" && e.key !== " ")) return;
                e.preventDefault();
                onSelect(agent.id, agentDisplayName(agent));
              }}
              className={`flex items-center gap-3 p-3 rounded-xl border bg-card cursor-pointer transition ${
                isMain
                  ? "opacity-45 cursor-not-allowed bg-[var(--cream-deep)]"
                  : isSelected
                    ? "border-[var(--midnight)] shadow-[0_0_0_3px_color-mix(in_srgb,var(--midnight)_8%,transparent)]"
                    : "border-[var(--edge)] hover:border-[var(--midnight-faint)] [@media(hover:hover)_and_(pointer:fine)]:hover:-translate-y-px"
              }`}
              title={isMain ? "main agent can't be published" : undefined}
            >
              <div className="w-9 h-9 rounded-md bg-cream-deeper text-[var(--midnight)] font-editorial-serif font-semibold text-sm flex items-center justify-center">
                {(agent.identity?.name ?? agent.name ?? "?").slice(0, 2)}
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium">{agentDisplayName(agent)}</div>
                {isMain && (
                  <div className="text-caption text-muted-foreground">core agent · not publishable</div>
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
      <p className="text-sm text-muted-foreground leading-snug mb-4">
        Help buyers find your agent. This is what they&apos;ll see in the marketplace.
      </p>
      <Field label="Public name">
        <input aria-label="Public name"
          value={form.name}
          onChange={(e) => onChange({ name: e.target.value })}
          className="border border-[var(--edge)] bg-cream-deep rounded-xl px-3.5 py-2.5 text-sm w-full"
        />
      </Field>
      <Field label="One-line tagline" hint="Max 80 characters.">
        <input aria-label="One-line tagline"
          maxLength={80}
          value={form.tagline}
          onChange={(e) => onChange({ tagline: e.target.value })}
          className="border border-[var(--edge)] bg-cream-deep rounded-xl px-3.5 py-2.5 text-sm w-full"
        />
      </Field>
      <Field label="Description" hint="Markdown allowed.">
        <textarea aria-label="Description"
          rows={4}
          value={form.description}
          onChange={(e) => onChange({ description: e.target.value })}
          className="border border-[var(--edge)] bg-cream-deep rounded-xl px-3.5 py-2.5 text-sm w-full min-h-[88px] resize-y"
        />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Category">
          <select aria-label="Category"
            value={form.category}
            onChange={(e) => onChange({ category: e.target.value })}
            className="border border-[var(--edge)] bg-cream-deep rounded-xl px-3.5 py-2.5 text-sm w-full"
          >
            {["Operations", "Sales", "Engineering", "Research", "Finance", "Marketing", "Personal"].map(
              (c) => (
                <option key={c}>{c}</option>
              ),
            )}
          </select>
        </Field>
        <Field label="Tags (max 5)">
          <div className="flex gap-1.5 flex-wrap items-center p-2 bg-cream-deep border border-[var(--edge)] rounded-xl min-h-[44px]">
            {form.tags.map((t) => (
              <span
                key={t}
                className="bg-cream-deeper text-[var(--midnight)] text-xs px-2.5 py-1 rounded-full inline-flex items-center gap-1.5"
              >
                {t}
                <button
                  type="button"
                  onClick={() => onChange({ tags: form.tags.filter((x) => x !== t) })}
                  aria-label={`Remove tag ${t}`}
                  className="text-muted-foreground hover:text-[var(--midnight)]"
                >
                  ×
                </button>
              </span>
            ))}
            <input aria-label="Add tag"
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
      <label className="text-ui font-semibold text-[var(--midnight)]">{label}</label>
      {children}
      {hint && <span className="text-caption text-muted-foreground">{hint}</span>}
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
  bundled: "border border-[var(--ok)] text-[var(--ok)]",
  entries: "border border-[var(--warn)] text-[var(--warn)]", // amber — secrets to fill on buyer side
  workspace: "border border-[var(--edge)] text-[var(--midnight)]",
  extra_dirs: "border border-[var(--err)] text-[var(--err)]",
  unknown: "border border-[var(--edge)] text-muted-foreground",
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
      <p className="text-sm text-muted-foreground leading-snug mb-4">
        Pick which integrations ride along with your agent. Buyers will need to
        fill in their own credentials for any MCP or configured skill.
      </p>

      {isLoading && <div className="text-ui text-muted-foreground">Loading…</div>}
      {error && (
        <div role="alert" className="text-ui text-[var(--err)] mb-3">
          Couldn&apos;t load preview: {error}
        </div>
      )}

      {preview && (
        <>
          <section className="mb-5" data-testid="bundle-mcps">
            <h4 className="text-ui font-semibold text-[var(--midnight)] mb-2">
              MCP servers
            </h4>
            {preview.available_mcps.length === 0 ? (
              <p className="text-ui text-muted-foreground">
                No MCP servers configured on your container.
              </p>
            ) : (
              <ul className="space-y-1.5">
                {preview.available_mcps.map((name) => (
                  <li key={name}>
                    <label className="flex items-center gap-2 cursor-pointer text-ui">
                      <input
                        type="checkbox"
                        checked={form.bundleMcps.includes(name)}
                        onChange={() => toggleMcp(name)}
                        aria-label={`bundle MCP ${name}`}
                      />
                      <span className="font-mono text-ui">{name}</span>
                      <span className="text-2xs px-1.5 py-0.5 rounded-full border border-[var(--warn)] text-[var(--warn)]">
                        secrets scrubbed
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="mb-5" data-testid="bundle-skills">
            <h4 className="text-ui font-semibold text-[var(--midnight)] mb-2">
              Skills
            </h4>
            {preview.available_skills.length === 0 &&
            preview.auto_ship_skills.length === 0 ? (
              <p className="text-ui text-muted-foreground">
                This agent has no skills configured.
              </p>
            ) : (
              <>
                {preview.available_skills.length > 0 && (
                  <ul className="space-y-1.5">
                    {preview.available_skills.map((s) => (
                      <li key={s.name}>
                        <label className="flex items-center gap-2 cursor-pointer text-ui">
                          <input
                            type="checkbox"
                            checked={form.bundleSkills.includes(s.name)}
                            onChange={() => toggleSkill(s.name)}
                            aria-label={`bundle skill ${s.name}`}
                          />
                          <span className="font-mono text-ui">{s.name}</span>
                          <span
                            className={`text-2xs px-1.5 py-0.5 rounded-full ${BUCKET_TONE[s.bucket]}`}
                            title={`bucket: ${s.bucket}`}
                          >
                            {BUCKET_LABEL[s.bucket]}
                          </span>
                        </label>
                      </li>
                    ))}
                  </ul>
                )}

                {preview.auto_ship_skills.length > 0 && (
                  <div
                    className={preview.available_skills.length > 0 ? "mt-3" : ""}
                    data-testid="bundle-auto-ship-skills"
                  >
                    <p className="text-caption text-muted-foreground mb-1.5">
                      Skills in this agent&apos;s workspace ship automatically — no
                      selection needed.
                    </p>
                    <div className="flex flex-wrap gap-1.5">
                      {preview.auto_ship_skills.map((name) => (
                        <span
                          key={name}
                          className={`font-mono text-caption px-2 py-0.5 rounded-full ${BUCKET_TONE.workspace}`}
                        >
                          {name}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </>
            )}
          </section>

          {preview.persona_files.length > 0 && (
            <section className="mb-2" data-testid="bundle-persona-files">
              <h4 className="text-ui font-semibold text-[var(--midnight)] mb-1.5">
                Personality files (always included)
              </h4>
              <p className="text-caption text-muted-foreground mb-1.5">
                These ship as authored. Review your wording before submitting —
                anything you wrote, your buyer reads.
              </p>
              <div className="flex flex-wrap gap-1.5">
                {preview.persona_files.map((name) => (
                  <span
                    key={name}
                    className="font-mono text-caption px-2 py-0.5 rounded-full border border-[var(--ok)] text-[var(--ok)]"
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
      ? "text-[var(--err)] border-[var(--err)]"
      : flag.severity === "medium"
        ? "text-[var(--warn)] border-[var(--warn)]"
        : "bg-cream-deep text-muted-foreground border-[var(--edge)]";
  return (
    <li className={`border rounded-lg px-2.5 py-1.5 text-xs ${tone}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-caption">{flag.pattern}</span>
        <span className="font-mono text-caption opacity-80">
          {flag.file}
          {flag.line ? `:${flag.line}` : ""}
        </span>
      </div>
      <div className="font-mono text-caption opacity-90 mt-0.5 break-all">
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
      // Web: navigates away. Desktop: opens the system browser (the webview
      // must not navigate to Stripe onboarding); status refreshes on return.
      await openAuthUrl(r.onboarding_url);
    } catch (err) {
      notifyApiError(err, "Couldn't open payout setup");
    } finally {
      setPayoutBusy(false);
    }
  }

  return (
    <div>
      <p className="text-sm text-muted-foreground leading-snug mb-4">
        Set what your work is worth. Price is hidden from the marketplace browse view —
        buyers only see it at install time.
      </p>

      <div className="bg-cream-deep rounded-xl p-1 flex mb-4">
        <button
          type="button"
          onClick={() => onChange({ priceTier: "free" })}
          className={`flex-1 px-3.5 py-2.5 rounded-xl text-sm ${
            form.priceTier === "free"
              ? "bg-card text-[var(--midnight)] shadow-sm font-semibold"
              : "text-muted-foreground"
          }`}
        >
          Free
        </button>
        <button
          type="button"
          onClick={() => onChange({ priceTier: "paid" })}
          className={`flex-1 px-3.5 py-2.5 rounded-xl text-sm ${
            form.priceTier === "paid"
              ? "bg-card text-[var(--midnight)] shadow-sm font-semibold"
              : "text-muted-foreground"
          }`}
        >
          Premium tier
        </button>
      </div>

      {needsPayoutSetup && (
        <div
          role="alert"
          data-testid="payout-setup-gate"
          className="mb-4 rounded-xl border border-[var(--warn)] px-3.5 py-3 text-ui text-[var(--warn)] leading-snug"
        >
          <strong className="block text-[var(--midnight)] mb-0.5">
            Set up payouts first.
          </strong>
          We can&apos;t charge buyers without a Stripe Connect account on file.
          You can finish the rest of the listing first, but submitting at a
          premium tier requires payouts.
          <button
            type="button"
            onClick={startPayoutOnboarding}
            disabled={payoutBusy}
            className="block mt-2 bg-[var(--midnight)] hover:bg-[var(--midnight-soft)] disabled:opacity-50 text-[var(--cream)] px-3.5 py-1.5 rounded-full text-ui font-semibold"
          >
            {payoutBusy ? "Opening Stripe…" : "Set up payouts →"}
          </button>
        </div>
      )}

      {form.priceTier === "free" ? (
        <div className="bg-card border border-[var(--edge)] rounded-xl p-4 text-muted-foreground text-ui leading-snug">
          <strong className="text-[var(--midnight)]">Free agents install instantly.</strong>
          <br />
          No payout setup needed. You can switch this listing to a premium tier anytime.
        </div>
      ) : (
        <>
          <label className="block text-ui font-semibold text-[var(--midnight)] mb-1.5">
            Price per install (USD)
          </label>
          <div className="flex items-center bg-cream-deep border border-[var(--edge)] rounded-xl px-3.5 py-3 gap-2 mb-2">
            <span className="font-editorial-serif text-2xl text-muted-foreground">$</span>
            <input aria-label="Price per install in dollars, one-time"
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
            <span className="text-xs text-muted-foreground">USD · one-time, perpetual license</span>
          </div>
          <p className="text-caption text-muted-foreground mb-3">
            Suggested range: $5–$20. The maximum is $20.
          </p>
          <div className="bg-card border border-[var(--edge)] rounded-xl px-4 py-3.5 text-ui">
            <Row label="List price" value={`$${v.toFixed(2)}`} />
            <Row label="Stripe processing fee (2.9% + $0.30)" value={`−$${stripe.toFixed(2)}`} />
            <Row label="Lightbulb platform fee (15%)" value={`−$${platform.toFixed(2)}`} />
            <div className="flex justify-between border-t border-[var(--edge)] mt-1.5 pt-2.5 text-[var(--midnight)] font-semibold">
              <span>Your take-home per install</span>
              <span className="font-editorial-serif text-[var(--midnight)] text-base">
                ${take.toFixed(2)}
              </span>
            </div>
          </div>
          <p className="text-xs italic text-muted-foreground mt-3">
            Earnings accrue to your seller balance. Cash out anytime from My Listings.
          </p>
        </>
      )}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between text-muted-foreground py-0.5">
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
      <p className="text-sm text-muted-foreground leading-snug mb-4">
        Here&apos;s how your listing will appear. Submit when ready — listings go live after
        admin moderation (usually within 24h).
      </p>
      <div className="bg-card border border-[var(--edge)] rounded-xl p-5">
        <div className="font-editorial-serif text-xl">{form.name || "Untitled"}</div>
        <div className="text-xs text-muted-foreground italic mt-0.5">by you</div>
        <p className="text-ui text-muted-foreground leading-snug mt-2">{form.tagline || "—"}</p>
        {form.tags.length > 0 && (
          <div className="flex gap-1.5 flex-wrap mt-3">
            {form.tags.map((t) => (
              <span
                key={t}
                className="bg-cream-deep text-muted-foreground text-caption px-2 py-0.5 rounded-full"
              >
                {t}
              </span>
            ))}
          </div>
        )}
        <div className="flex justify-between text-ui text-muted-foreground mt-3 pt-3 border-t border-dashed border-[var(--edge)]">
          <span data-testid="review-bundle-summary">
            Bundled: {mcpCount} MCP{mcpCount === 1 ? "" : "s"} · {skillCount} skill
            {skillCount === 1 ? "" : "s"} · {personaCount} persona file
            {personaCount === 1 ? "" : "s"}
          </span>
          <span className="font-semibold text-[var(--midnight)]">
            {form.priceTier === "free" ? "Free" : "Premium tier"}
          </span>
        </div>
      </div>

      {needsConfigPatch && (
        <div
          role="status"
          data-testid="review-config-patch-warning"
          className="mt-3 rounded-xl border border-[var(--warn)] px-3.5 py-2.5 text-ui text-[var(--warn)] leading-snug"
        >
          Heads up — installing this listing will add to your buyer&apos;s
          agent configuration (MCPs / configured skills). Buyers will need to fill in
          their own credentials before those integrations work.
        </div>
      )}

      {safetyFlags.length > 0 && (
        <section
          className="mt-3"
          aria-label="Safety scan findings"
          data-testid="review-safety-flags"
        >
          <h4 className="text-ui font-semibold text-[var(--midnight)] mb-1.5">
            Safety scan: {safetyFlags.length} finding{safetyFlags.length === 1 ? "" : "s"}
          </h4>
          <p className="text-caption text-muted-foreground mb-2">
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

      <p className="text-xs text-muted-foreground leading-snug mt-3">
        By publishing, you agree to the{" "}
        <a href="/terms" target="_blank" rel="noreferrer" className="text-[var(--midnight)] underline">
          Marketplace Terms
        </a>{" "}
        and confirm this agent is yours to publish. Lightbulb retains the right to remove listings
        that violate the{" "}
        <a
          href="/acceptable-use"
          target="_blank"
          rel="noreferrer"
          className="text-[var(--midnight)] underline"
        >
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
      <div className="text-2xl mb-3">✓</div>
      <h3 className="font-editorial-serif text-2xl mb-2">
        {mode === "resubmit" ? "Resubmitted for review." : "Submitted for review."}
      </h3>
      <p className="text-sm text-muted-foreground mb-4">
        You&apos;ll get a notification when it&apos;s live — usually within 24 hours.
      </p>
      <button
        onClick={onViewMine}
        className="bg-[var(--midnight)] hover:bg-[var(--midnight-soft)] text-[var(--cream)] px-5 py-2 rounded-full font-semibold"
      >
        View my listings →
      </button>
    </div>
  );
}
