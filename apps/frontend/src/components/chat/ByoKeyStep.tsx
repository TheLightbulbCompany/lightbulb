"use client";
import { useState } from "react";
import { useApi } from "@/lib/api";

type Provider = "openai" | "anthropic";
type Props = { onComplete: () => void };

// Onboarding design system — mirrors the stepper branch this step renders
// inside (cream page, forest CTAs, Lora headings, DM Sans body). Fonts ride
// the next/font CSS vars via inline styles rather than Tailwind font classes,
// the same way the stepper's own cards do.
const SERIF_FONT = { fontFamily: "var(--font-lora-serif), serif" } as const;
const SANS_FONT = { fontFamily: "var(--font-dm-sans), sans-serif" } as const;

// Segmented pill selector. The real radio stays in the DOM (sr-only) so
// keyboard + screen-reader behaviour is untouched; the pill is the peer's
// visual skin. `peer-checked:hover:` re-asserts the selected fill because
// Tailwind orders the bare `hover:` variant after `checked:`.
const PROVIDER_PILL =
  "block cursor-pointer rounded-full border px-4 py-2 text-sm transition-colors " +
  "border-[#e0dbd0] bg-white text-[#1a1a1a] hover:bg-[#f3efe6] " +
  "peer-checked:border-[#06402B] peer-checked:bg-[#06402B] peer-checked:text-white " +
  "peer-checked:hover:bg-[#06402B] " +
  "peer-focus-visible:ring-2 peer-focus-visible:ring-[rgba(6,64,43,0.25)]";

const PRIMARY_BUTTON =
  "rounded-full bg-[#06402B] px-6 py-2.5 text-sm font-medium text-white " +
  "transition-colors hover:bg-[#052e1f] disabled:opacity-50 disabled:hover:bg-[#06402B]";

export function ByoKeyStep({ onComplete }: Props) {
  const api = useApi();
  const [provider, setProvider] = useState<Provider>("openai");
  const [apiKey, setApiKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      // Backend route: PUT /settings/keys/{tool_id} with {api_key}
      // (See routers/settings_keys.py.)
      await api.put(`/settings/keys/${provider}`, { api_key: apiKey });
      // Persist the user's provider choice so the gateway knows this is a
      // card-2 user (BYO-key cards never get gated on credits — Plan 3
      // Tasks 4 + 5). ByoKeyStep is the only step that knows the chosen
      // sub-provider (openai vs anthropic), so the wizard delegates the
      // /users/sync call here rather than passing byo_provider up.
      await api.post("/users/sync", {
        provider_choice: "byo_key",
        byo_provider: provider,
      });
      onComplete();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to save key");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={submit}
      className="flex flex-col gap-4 py-8 max-w-md mx-auto"
      style={SANS_FONT}
    >
      <h3 className="text-[22px] font-normal text-[#1a1a1a]" style={SERIF_FONT}>
        Bring your own API key
      </h3>
      <p className="text-sm text-[#8a8578]">
        Use your own OpenAI or Anthropic account. We never see your key
        after you save it &mdash; it&apos;s stored encrypted in AWS Secrets
        Manager and injected into your container at runtime.
      </p>

      <fieldset className="flex gap-3">
        <label className="relative cursor-pointer">
          <input
            type="radio"
            name="provider"
            value="openai"
            checked={provider === "openai"}
            onChange={() => setProvider("openai")}
            className="peer sr-only"
          />
          <span className={PROVIDER_PILL}>OpenAI</span>
        </label>
        <label className="relative cursor-pointer">
          <input
            type="radio"
            name="provider"
            value="anthropic"
            checked={provider === "anthropic"}
            onChange={() => setProvider("anthropic")}
            className="peer sr-only"
          />
          <span className={PROVIDER_PILL}>Anthropic</span>
        </label>
      </fieldset>

      <input
        type="password"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        placeholder={
          provider === "openai" ? "sk-proj-..." : "sk-ant-..."
        }
        required
        autoComplete="off"
        spellCheck={false}
        className="rounded-lg border border-[#e0dbd0] bg-white px-3 py-2.5 font-mono text-sm text-[#1a1a1a] placeholder:text-[#b5ae9e] focus:outline-none focus:border-[rgba(6,64,43,0.25)] focus:ring-2 focus:ring-[rgba(6,64,43,0.25)]"
      />

      {error && <p className="text-sm text-[#b32424]">{error}</p>}

      <button
        type="submit"
        disabled={submitting || !apiKey}
        className={PRIMARY_BUTTON}
        style={SANS_FONT}
      >
        {submitting ? "Validating…" : "Save key"}
      </button>
    </form>
  );
}
