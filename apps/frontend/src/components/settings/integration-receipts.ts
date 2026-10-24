/**
 * The "delivery receipt": what connecting a provider actually gives the
 * agent, in user language. Keyed by the registry's provider id. Static
 * frontend copy for now — when the backend registry grows per-provider
 * delivery metadata, this map is the seam to replace.
 */
export interface IntegrationReceipt {
  /** Sentence fragment after "Delivered"/"Will deliver" — no trailing period. */
  delivers: string;
}

export const INTEGRATION_RECEIPTS: Record<string, IntegrationReceipt> = {
  stripe: { delivers: "payment tools — look up customers, invoices & charges" },
  notion: { delivers: "Notion page search & editing tools" },
  sentry: { delivers: "issue search & triage tools for your projects" },
  linear: { delivers: "Linear issue & project tools" },
  github: { delivers: "repo, issue & PR tools scoped to your account" },
  google: { delivers: "sending mail, Calendar events & Drive files it creates or you pick" },
  microsoft365: { delivers: "read-only Outlook mail, calendar & OneDrive/SharePoint files for your work, school or personal Microsoft account" },
  vercel_cli: { delivers: "deploy, logs, project & env tools for your Vercel account" },
  aws: { delivers: "read-only AWS access — describe & list your account's resources" },
  instagram: { delivers: "publish posts & Reels, moderate comments & answer DMs on your Instagram" },
  x: { delivers: "X posting, reading, search, replies, DMs, follows, likes & media through xurl" },
  meta_ads: {
    delivers: "campaign, ad set & creative management plus performance insights for your ad accounts",
  },
  posthog: { delivers: "product analytics — query events, funnels & session recordings" },
  intercom: { delivers: "conversation search & reply tools for your Intercom inbox" },
  quickbooks: {
    delivers: "your books — invoices, bills, customers, P&L & balance sheet for one QuickBooks company",
  },
  attio: { delivers: "CRM record search & editing across your Attio workspace" },
  apollo: { delivers: "read-only Apollo access — people, company & contact search plus contact enrichment" },
  cloudflare: {
    delivers: "generated-JS access to your Cloudflare account — DNS, Workers, R2 & Zero Trust — instead of a fixed tool list",
  },
  parallel: { delivers: "web search powered by Parallel's API in place of the default provider" },
  shopify: { delivers: "Shopify Admin API access to your store — orders, products, inventory & customers" },
};
