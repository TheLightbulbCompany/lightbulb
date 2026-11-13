/**
 * The predicate Settings' provider card uses on `/billing/account`: does a
 * null `provider_choice` mean managed inference, or genuinely no provider?
 *
 * Mirrors the backend's own fallback (`provider_choice_from_row`,
 * apps/backend/core/services/container_provision.py): a cardless free
 * trial, and a paid Base/Workspace/Ultra row that never picked a provider,
 * both run on Lightbulb-managed DeepSeek within a daily allowance rather
 * than having "no provider selected".
 */
export interface ManagedProviderAccountFields {
  provider_choice: string | null;
  subscription_status?: string | null;
  /** "base" | "standard" (Workspace) | "ultra_pro" (Ultra) | null. */
  plan?: string | null;
}

export function isManagedNoChoiceAccount(account: ManagedProviderAccountFields): boolean {
  return (
    account.provider_choice == null &&
    (account.subscription_status === "trial_local" ||
      account.plan === "base" ||
      account.plan === "standard" ||
      account.plan === "ultra_pro")
  );
}

/** Matches web's `LLMPanel.tsx` `ManagedAllowanceBlock` hero copy. */
export const MANAGED_NO_CHOICE_LABEL = {
  title: "Lightbulb-managed models",
  detail: "DeepSeek within a daily allowance",
} as const;
