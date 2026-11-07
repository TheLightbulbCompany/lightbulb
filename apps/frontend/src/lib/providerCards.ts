/**
 * Shared provider-card metadata for the offered provider paths.
 *
 * Used by:
 *   - `components/landing/PricingThreeCard.tsx` — renders cards with
 *     anchor links to /sign-up?provider=…
 *   - `components/chat/ProvisioningStepper.tsx#ProviderPicker` — renders an
 *     informational provider comparison beside one workspace subscription
 *     CTA. Provider choice is made after entitlement.
 *
 * Previously: three near-identical card arrays lived inline across these
 * two surfaces. The landing copy is the canonical version; the
 * provisioning surface uses the same wording so pricing intent → checkout
 * doesn't read like a different product.
 */

export type ProviderChoiceId = "chatgpt_oauth" | "byo_key" | "openrouter_managed";

export interface ProviderCard {
  id: ProviderChoiceId;
  title: string;
  subtitle: string;
  trialNote: string;
  bullets: string[];
  cta: string;
  /** Landing-page anchor target. Provisioning surface ignores this. */
  href: string;
  highlighted?: boolean;
}

/**
 * In-app (post-signup) provider choices. Same paths as
 * `PROVIDER_CARDS`, minus the pricing/marketing copy — an owner picking here
 * already has a subscription, so the sales framing would be noise.
 *
 * Used by the provisioning stepper and the team billing card.
 */
export interface ProviderSwitchCard {
  id: ProviderChoiceId;
  title: string;
  subtitle: string;
}

export const PROVIDER_SWITCH_CARDS: ProviderSwitchCard[] = [
  {
    id: "chatgpt_oauth",
    title: "Sign in with ChatGPT",
    subtitle: "Inference via your ChatGPT subscription",
  },
  {
    id: "byo_key",
    title: "Bring your own API key",
    subtitle: "OpenAI or Anthropic — your key, your billing",
  },
];

export function providerSwitchCards(managedAvailable: boolean): ProviderSwitchCard[] {
  return managedAvailable
    ? [
        { id: "openrouter_managed", title: "Lightbulb-managed models", subtitle: "Uses prepaid workspace credits" },
        ...PROVIDER_SWITCH_CARDS,
      ]
    : PROVIDER_SWITCH_CARDS;
}

export const PROVIDER_CARDS: ProviderCard[] = [
  {
    id: "chatgpt_oauth",
    title: "Sign in with ChatGPT",
    subtitle: "Inference uses your separate ChatGPT subscription",
    trialNote: "Choose a provider after workspace billing is active",
    bullets: [
      "GPT-5.5 included via your ChatGPT account",
      "Always-on workspace",
    ],
    cta: "Get started",
    href: "/sign-up?provider=chatgpt_oauth",
    // Owner 2026-09-23: what every paying workspace uses; revisit for managed once paid credits ship.
    highlighted: true,
  },
  {
    id: "byo_key",
    title: "Bring your own API key",
    subtitle: "Your selected provider bills inference separately",
    trialNote: "Add your key after workspace billing is active",
    bullets: [
      "OpenAI or Anthropic — your key, your billing",
      "All channels",
      "Always-on workspace",
    ],
    cta: "Get started",
    href: "/sign-up?provider=byo_key",
  },
  {
    id: "openrouter_managed",
    title: "Lightbulb-managed",
    subtitle: "Included with every plan — DeepSeek within a daily allowance",
    trialNote: "On by default — nothing to set up",
    bullets: [
      "Free trial: DeepSeek within a daily allowance",
      "More models with prepaid credits — coming soon",
      "All channels",
    ],
    cta: "Get started",
    // No ?provider= — this IS the default a fresh sign-up lands on, so the
    // stepper's `?provider=` step (chatgpt_oauth | byo_key only) never fires.
    href: "/sign-up",
  },
];

/**
 * Pre-entitlement comparison (`ProviderPicker`). Deliberately built from only
 * the first two PROVIDER_CARDS entries, not all of them: the static
 * Lightbulb-managed card above is landing/marketing copy, and this surface's
 * own managed card (below, gated on `managedAvailable`) carries different,
 * post-entitlement-accurate copy (prepaid credits) — spreading PROVIDER_CARDS
 * wholesale would show both at once. Mirrors the pre-2026-09-22 pattern from
 * when a third PROVIDER_CARDS entry (bedrock_claude) last existed here.
 */
export function providerComparisonCards(managedAvailable: boolean): ProviderCard[] {
  const base = PROVIDER_CARDS.filter((card) => card.id !== "openrouter_managed");
  return managedAvailable
    ? [
        ...base,
        {
          id: "openrouter_managed",
          title: "Lightbulb-managed models",
          subtitle: "Managed inference uses prepaid workspace credits",
          trialNote: "Choose and fund managed inference separately after billing",
          bullets: ["Models approved by Lightbulb", "All channels", "Always-on container"],
          cta: "Get started",
          href: "/chat?panel=llm",
        },
      ]
    : base;
}
