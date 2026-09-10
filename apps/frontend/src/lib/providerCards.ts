/**
 * Shared provider-card metadata for the three provider paths.
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

export type ProviderChoiceId = "chatgpt_oauth" | "byo_key" | "bedrock_claude";

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
 * In-app (post-signup) provider choices. Same three paths as
 * `PROVIDER_CARDS`, minus the pricing/marketing copy — an owner picking here
 * already has a subscription, so the sales framing would be noise.
 *
 * Used by:
 *   - `ChangeProviderModal#PickerStage` — the deliberate switch flow.
 *   - `LLMPanel`'s empty state — the inline first-pick.
 */
export interface ProviderSwitchCard {
  id: ProviderChoiceId;
  title: string;
  subtitle: string;
}

export const PROVIDER_SWITCH_CARDS: ProviderSwitchCard[] = [
  {
    id: "bedrock_claude",
    title: "Powered by Claude",
    subtitle: "Bedrock-managed Claude credits",
  },
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

export const PROVIDER_CARDS: ProviderCard[] = [
  {
    id: "chatgpt_oauth",
    title: "Sign in with ChatGPT",
    subtitle: "Workspace subscription + your ChatGPT subscription",
    trialNote: "Sign in to OpenAI after checkout",
    bullets: [
      "GPT-5.5 included via your ChatGPT account",
      "All channels (Telegram, Discord, WhatsApp)",
      "Always-on container",
    ],
    cta: "Get started",
    href: "/sign-up?provider=chatgpt_oauth",
  },
  {
    id: "byo_key",
    title: "Bring your own API key",
    subtitle: "Workspace subscription + your provider bill",
    trialNote: "Add your key after checkout",
    bullets: [
      "OpenAI or Anthropic — your key, your billing",
      "All channels",
      "Always-on container",
    ],
    cta: "Get started",
    href: "/sign-up?provider=byo_key",
  },
  {
    id: "bedrock_claude",
    title: "Powered by Claude",
    // This path is the only one that needs funded credits before
    // provision_gate will start the container, so its Checkout carries a
    // one-time $20 credits line item ALONGSIDE the $50 subscription — one
    // session, one card entry, $70 shown before payment. Previously it was
    // two checkouts and the second was a surprise: an owner reached
    // CreditsStep having already paid $50 for a container that wouldn't boot.
    // If you change this copy, change BEDROCK_STARTER_CREDITS_CENTS in
    // billing_service.create_flat_fee_checkout with it — this string IS the
    // pre-payment disclosure.
    //
    // These two strings are the LANDING-page copy (PricingThreeCard). The
    // ProviderPicker overrides both with a total computed from the credit
    // amount the owner picks, because only that surface has the chooser.
    //
    // So they must be true with NO amount selected, and true on a page whose
    // cards link to /sign-up?provider=… — where a new signup gets a card-less
    // trial_local row (user_provision), not a charge. Hence "from $5" rather
    // than a fixed total, and "at checkout" rather than "due today".
    subtitle: "Workspace subscription + Claude credits from $5",
    trialNote: "Credits bought at checkout — no second step",
    bullets: [
      "Claude Sonnet 4.6 + Opus 4.7",
      "All channels",
      "Always-on container",
    ],
    cta: "Get started",
    href: "/sign-up?provider=bedrock_claude",
    highlighted: true,
  },
];
