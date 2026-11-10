/**
 * Shared metadata for the two customer-selectable inference paths.
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

export type ProviderChoiceId = "chatgpt_oauth" | "openrouter_managed";

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
];

export function providerSwitchCards(managedAvailable: boolean): ProviderSwitchCard[] {
  return managedAvailable
    ? [
        { id: "openrouter_managed", title: "OpenRouter managed", subtitle: "Uses prepaid workspace credits" },
        ...PROVIDER_SWITCH_CARDS,
      ]
    : PROVIDER_SWITCH_CARDS;
}

export const PROVIDER_CARDS: ProviderCard[] = [
  {
    id: "chatgpt_oauth",
    title: "Sign in with ChatGPT",
    subtitle: "Inference uses your separate ChatGPT subscription",
    trialNote: "Sign in after the workspace plan is active",
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
    id: "openrouter_managed",
    title: "OpenRouter managed",
    subtitle: "Included with every plan — DeepSeek within a daily allowance",
    trialNote: "Choose after the workspace plan is active",
    bullets: [
      "Free trial: DeepSeek within a daily allowance",
      "More models with prepaid credits — coming soon",
      "All channels",
    ],
    cta: "Get started",
    // Selection is only intent; checkout return resumes setup and the
    // authenticated /billing/provider write persists it afterward.
    href: "/sign-up?provider=openrouter_managed",
  },
];

/**
 * Pre-entitlement comparison (`ProviderPicker`). Both current paths are
 * always shown; `managedAvailable` only selects the accurate post-entitlement
 * managed copy when a paid workspace can use prepaid credits.
 */
export function providerComparisonCards(managedAvailable: boolean, chatgptAvailable = false): ProviderCard[] {
  const chatgpt = PROVIDER_CARDS.find((card) => card.id === "chatgpt_oauth")!;
  const managed = managedAvailable
    ? {
        id: "openrouter_managed" as const,
        title: "OpenRouter managed",
        subtitle: "Managed inference uses prepaid workspace credits",
        trialNote: "Selection is saved after checkout; prepaid credits are separate",
        bullets: ["Models approved by Lightbulb", "All channels", "Always-on workspace"],
        cta: "Get started",
        href: "/chat?panel=llm",
      }
    : PROVIDER_CARDS.find((card) => card.id === "openrouter_managed")!;
  // ChatGPT sign-in is internal-only: the server says per workspace whether to offer it.
  return chatgptAvailable ? [managed, chatgpt] : [managed];
}
