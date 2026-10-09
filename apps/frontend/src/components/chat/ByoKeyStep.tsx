"use client";
/**
 * BYO-key provider step.
 *
 * Rendered by ProvisioningStepper for `?provider=byo_key` — reached either
 * from the signup wizard (provider picker / landing CTA) or from the
 * `byo_provider_required` gate's own remedy link. Two backend calls, in this
 * order:
 *
 *   1. PUT /settings/keys/{provider} — stores the key encrypted (Secrets
 *      Manager), injected into the container at runtime.
 *   2. POST /billing/provider — writes `provider_choice`/`byo_provider` onto
 *      the BILLING row. That row field, not whether a key exists, is what
 *      the provision gate reads (core/services/provision_gate.py's byo_key
 *      branch), and /billing/provider is its only post-signup writer. Skip
 *      it and the gate re-fires on the next /container/status poll, routing
 *      the user straight back to this step — forever.
 *
 * The order is load-bearing in the other direction too: billing_service's
 * change_provider rejects with 409 `byo_key_missing` unless the key is
 * already saved, so key-first is the designed sequence.
 */
import { useCallback, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ApiError, useApi } from "@/lib/api";
import { replaceSearchParams } from "@/lib/shellNav";

type Provider = "openai" | "anthropic";
type Props = {
  /**
   * Fired once the key is saved AND the provider choice is committed —
   * or, in the pre-subscription wizard case, once the key is saved and the
   * commit is legitimately deferred (see PRE_SUBSCRIPTION_CODES).
   */
  onComplete: () => void;
};

// Same copy the stepper's blocked card shows a non-admin member, so a 403
// here reads identically to the generic interstitial they'd otherwise get.
const ADMIN_ONLY_MESSAGE = "Ask your org admin to fix this.";

/**
 * change_provider's pre-subscription rejections.
 *
 * This step is reachable BEFORE a subscription exists: the stepper's phase
 * machine lets a `trial_local` owner (and any owner mid-signup) land on the
 * provider step, and change_provider refuses to write on those rows. That is
 * not something the user can act on — the key IS saved, and once they
 * subscribe the gate fires `byo_provider_required` once and routes them back
 * here, where the write lands. Blocking the wizard on it would invent a new
 * dead end while fixing the old one, so these fail SOFTLY: no error, and
 * onComplete() still runs. Every other failure stays hard + retryable.
 */
const PRE_SUBSCRIPTION_CODES = new Set([
  "no_billing_account", // 404 — no billing row yet
  "use_trial_checkout", // 409 — trial_local picks its provider at checkout
  "subscription_inactive", // 409 — subscription not active (yet)
]);

/**
 * FastAPI's `detail` is a plain string for most raises but a structured
 * `{code, message}` for billing's ChangeProviderError — and the shared API
 * client only lifts the string form onto `ApiError.message` (an object
 * detail leaves it as the useless "API 409"). Read both shapes.
 */
function errorDetail(err: ApiError): { code: string | null; message: string | null } {
  const detail = (err.body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return { code: null, message: detail };
  if (detail && typeof detail === "object") {
    const { code, message } = detail as { code?: unknown; message?: unknown };
    return {
      code: typeof code === "string" ? code : null,
      message: typeof message === "string" ? message : null,
    };
  }
  return { code: null, message: null };
}

/** True for the rejections that mean "not subscribed yet", not "this failed". */
function isPreSubscriptionRejection(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  const { code } = errorDetail(err);
  if (code) return PRE_SUBSCRIPTION_CODES.has(code);
  // No structured code to read — a bare 404 from this route can only be the
  // missing-billing-row raise, so treat it the same way.
  return err.status === 404;
}

function saveErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    // BOTH routes require_org_admin server-side (settings_keys.set_key and
    // billing.change_provider) while the gate that sends users here reports
    // admin_only=false — so a non-admin org member DOES reach this step and
    // WILL 403, on either call. Give them the out-of-band route rather than
    // a raw "Organization admin access required".
    if (err.status === 403) return ADMIN_ONLY_MESSAGE;
    // change_provider's other 409s (e.g. byo_key_missing) carry user-facing
    // copy inside a structured detail — surface it verbatim.
    const { message } = errorDetail(err);
    if (message) return message;
  }
  return err instanceof Error ? err.message : "Failed to save key";
}

// Onboarding design system — mirrors the stepper branch this step renders
// inside (cream page, forest CTAs, Lora headings, DM Sans body). Fonts ride
// the next/font CSS vars via inline styles rather than Tailwind font classes,
// the same way the stepper's own cards do.
const SERIF_FONT = { fontFamily: "var(--font-serif), serif" } as const;
const SANS_FONT = { fontFamily: "var(--font-sans), sans-serif" } as const;

// Segmented pill selector. The real radio stays in the DOM (sr-only) so
// keyboard + screen-reader behaviour is untouched; the pill is the peer's
// visual skin. `peer-checked:hover:` re-asserts the selected fill because
// Tailwind orders the bare `hover:` variant after `checked:`.
const PROVIDER_PILL =
  "block cursor-pointer rounded-full border px-4 py-2 text-sm transition-colors " +
  "border-[var(--edge)] bg-cream text-[var(--midnight)] hover:bg-[var(--cream-deep)] " +
  "peer-checked:border-[var(--midnight)] peer-checked:bg-[var(--midnight)] peer-checked:text-cream " +
  "peer-checked:hover:bg-[var(--midnight)] " +
  "peer-focus-visible:ring-2 peer-focus-visible:ring-ring";

const PRIMARY_BUTTON =
  "rounded-full bg-[var(--midnight)] px-6 py-2.5 text-sm font-medium text-cream " +
  "transition-colors hover:bg-[var(--midnight-soft)] disabled:bg-cream-deep disabled:text-[var(--midnight-muted)] disabled:hover:bg-cream-deep";

export function ByoKeyStep({ onComplete }: Props) {
  const api = useApi();
  const searchParams = useSearchParams();
  const [provider, setProvider] = useState<Provider>("openai");
  const [apiKey, setApiKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The stepper's only "go back" mechanism is the `?provider=` URL param:
  // stripping it exits the provider step and re-enters the main flow (the
  // provider picker for a not-yet-subscribed owner, normal provisioning
  // otherwise). Mirrors CreditsStep.chooseDifferentProvider so other params
  // survive — without it a user who picked BYO by mistake is stranded on this
  // form with no way back to the other providers.
  const chooseDifferentProvider = useCallback(() => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("provider");
    replaceSearchParams(params);
  }, [searchParams]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      // Backend route: PUT /settings/keys/{tool_id} with {api_key}
      // (See routers/settings_keys.py.)
      await api.put(`/settings/keys/${provider}`, { api_key: apiKey });
      // Commit the provider choice onto the billing row. ByoKeyStep is the
      // only step that knows the chosen sub-provider (openai vs anthropic),
      // so the wizard delegates this write here rather than passing
      // byo_provider up.
      //
      // It MUST be /billing/provider: POST /users/sync still ACCEPTS a
      // {provider_choice, byo_provider} body for old clients but SILENTLY
      // IGNORES both fields server-side (routers/users.py says so verbatim),
      // so sending it there wrote nothing and left byo_provider_required
      // blocking. Do not re-add that call — the user record upsert it also
      // performs is already owned by useUserSyncOnce.
      try {
        await api.post("/billing/provider", {
          provider_choice: "byo_key",
          byo_provider: provider,
        });
      } catch (err: unknown) {
        // Pre-subscription rejections are expected on the wizard path and
        // must not strand the user; anything else is a real failure.
        if (!isPreSubscriptionRejection(err)) throw err;
      }
      onComplete();
    } catch (err: unknown) {
      // The key PUT is idempotent and already durable if it was step 1 that
      // succeeded, so leaving the form enabled makes a retry resume from here.
      setError(saveErrorMessage(err));
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
      <h3 className="text-title font-normal text-[var(--midnight)]" style={SERIF_FONT}>
        Bring your own API key
      </h3>
      <p className="text-sm text-[var(--midnight-muted)]">
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

      <input aria-label="API key"
        type="password"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        placeholder={
          provider === "openai" ? "sk-proj-..." : "sk-ant-..."
        }
        required
        autoComplete="off"
        spellCheck={false}
        className="rounded-lg border border-[var(--edge)] bg-cream-deep px-3 py-2.5 font-mono text-sm text-[var(--midnight)] placeholder:text-[var(--midnight-muted)]"
      />

      {error && <p className="text-sm text-[var(--err)]">{error}</p>}

      <button
        type="submit"
        disabled={submitting || !apiKey}
        className={PRIMARY_BUTTON}
        style={SANS_FONT}
      >
        {submitting ? "Validating…" : "Save key"}
      </button>

      <button
        type="button"
        onClick={chooseDifferentProvider}
        disabled={submitting}
        className="text-xs text-[var(--midnight-muted)] underline hover:text-[var(--midnight)] disabled:opacity-50"
        style={SANS_FONT}
      >
        Choose a different provider
      </button>
    </form>
  );
}
