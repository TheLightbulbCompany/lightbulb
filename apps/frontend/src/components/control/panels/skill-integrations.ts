/**
 * Skills that pair with a curated integration. Skills are the "how"
 * (workflow knowledge); the integration is the "ability" (an authenticated
 * account) — a skill is MORE useful once its provider is connected, never
 * redundant. Keyed by skill name → curated provider id + display label.
 */
export const SKILL_INTEGRATIONS: Record<string, { provider: string; label: string }> = {
  github: { provider: "github", label: "GitHub" },
  "gh-issues": { provider: "github", label: "GitHub" },
  gog: { provider: "google", label: "Google" },
  notion: { provider: "notion", label: "Notion" },
  stripe: { provider: "stripe", label: "Stripe" },
};
