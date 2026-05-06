import type { CatalogAgent } from "@/hooks/useCatalog";
import type { Agent as InstalledAgent } from "@/hooks/useAgents";
import { agentDisplayName, getAgentModelString } from "@/hooks/useAgents";

export interface EditorialAgent {
  slug: string;
  name: string;
  role: string;
  monogram: string;
  accent: string;
  tint: string;
  vibe: string;
  description: string;
  category: string;
  featured: boolean;
  new: boolean;
  installs: string;
  rating: number;
  skills: string[];
  channels: string[];
  suggested_model: string;
  quote: string;
}

export const AGENT_CATEGORIES = [
  { id: "all", label: "All" },
  { id: "Operations", label: "Operations" },
  { id: "Engineering", label: "Engineering" },
  { id: "Research", label: "Research" },
  { id: "Support", label: "Support" },
  { id: "Sales", label: "Sales" },
  { id: "Marketing", label: "Marketing" },
  { id: "Finance", label: "Finance" },
  { id: "Data", label: "Data" },
  { id: "People", label: "People" },
  { id: "Security", label: "Security" },
] as const;

export const EDITORIAL_AGENTS: EditorialAgent[] = [
  {
    slug: "atlas",
    name: "Atlas",
    role: "Inbox triage",
    monogram: "At",
    accent: "#06402B",
    tint: "#e6efe9",
    vibe: "Clears your queue before standup. Drafts, defers, dispatches.",
    description:
      "Atlas reads every thread that lands in your shared inboxes, ranks by urgency, drafts replies in your tone, and routes anything escalation-worthy to the right teammate.",
    category: "Operations",
    featured: true,
    new: false,
    installs: "12.4k",
    rating: 4.9,
    skills: ["gmail.send", "gmail.thread", "linear.create", "slack.dm"],
    channels: ["Gmail", "Slack", "Linear"],
    suggested_model: "claude-sonnet-4",
    quote: "Reply, route, or rest - never hesitate.",
  },
  {
    slug: "marlowe",
    name: "Marlowe",
    role: "Research analyst",
    monogram: "Mr",
    accent: "#3a4a8a",
    tint: "#e8eaf3",
    vibe: "A patient librarian who actually reads the footnotes.",
    description:
      "Long-form deep research. Marlowe synthesizes sources into a citable brief, cross-checks claims, and flags disagreements instead of papering over them.",
    category: "Research",
    featured: true,
    new: true,
    installs: "8.1k",
    rating: 4.8,
    skills: ["web.search", "web.read", "notion.write", "pdf.read"],
    channels: ["Notion", "Slack"],
    suggested_model: "claude-opus-4",
    quote: "Every claim earns a citation.",
  },
  {
    slug: "porter",
    name: "Porter",
    role: "On-call engineer",
    monogram: "Po",
    accent: "#8a3a3a",
    tint: "#f3e8e8",
    vibe: "First responder. Pages humans only when the stack actually panics.",
    description:
      "Porter subscribes to alerting webhooks, correlates logs and metrics, posts a triage thread, and escalates only when the playbook cannot resolve it autonomously.",
    category: "Engineering",
    featured: true,
    new: false,
    installs: "21.2k",
    rating: 4.7,
    skills: ["pagerduty.ack", "datadog.query", "github.commit", "slack.post"],
    channels: ["PagerDuty", "Slack", "GitHub"],
    suggested_model: "claude-sonnet-4",
    quote: "Triage now. Postmortem later.",
  },
  {
    slug: "june",
    name: "June",
    role: "Customer relations",
    monogram: "Ju",
    accent: "#a35d1f",
    tint: "#f3ebe0",
    vibe: "Calm, kind, never copy-pastes a canned response.",
    description:
      "June handles tier-1 and tier-2 support, references customer history, and hands off with full context when something needs a human touch.",
    category: "Support",
    featured: false,
    new: false,
    installs: "15.7k",
    rating: 4.9,
    skills: ["zendesk.reply", "stripe.refund", "intercom.search"],
    channels: ["Zendesk", "Intercom"],
    suggested_model: "claude-sonnet-4",
    quote: "The customer is a person, not a ticket.",
  },
  {
    slug: "wren",
    name: "Wren",
    role: "Recruiter",
    monogram: "Wr",
    accent: "#5a3a8a",
    tint: "#ebe6f3",
    vibe: "Sources, screens, schedules - and never ghosts a candidate.",
    description:
      "Wren pulls candidates from your ATS, runs first-round screens, schedules onsites, and writes after-action notes for hiring managers.",
    category: "People",
    featured: false,
    new: true,
    installs: "3.3k",
    rating: 4.6,
    skills: ["greenhouse.search", "calendly.book", "linkedin.message"],
    channels: ["Greenhouse", "Email"],
    suggested_model: "claude-sonnet-4",
    quote: "Every candidate gets a real reply.",
  },
  {
    slug: "fern",
    name: "Fern",
    role: "Bookkeeper",
    monogram: "Fe",
    accent: "#2d6b4e",
    tint: "#e3eee7",
    vibe: "Reconciles ledgers like it's a meditation practice.",
    description:
      "Fern categorizes transactions, reconciles statements across payment systems, and flags anomalies before your accountant has to ask.",
    category: "Finance",
    featured: false,
    new: false,
    installs: "6.9k",
    rating: 4.8,
    skills: ["stripe.read", "mercury.read", "quickbooks.write"],
    channels: ["QuickBooks", "Slack"],
    suggested_model: "claude-haiku-4",
    quote: "Reconciled, to the cent.",
  },
  {
    slug: "soren",
    name: "Soren",
    role: "PR reviewer",
    monogram: "So",
    accent: "#1f5a7a",
    tint: "#e0ecf1",
    vibe: "A second pair of eyes that actually reads the diff.",
    description:
      "Soren reviews pull requests against your style guide, leaves inline comments, and approves the small stuff so humans can focus on architecture.",
    category: "Engineering",
    featured: false,
    new: false,
    installs: "18.2k",
    rating: 4.8,
    skills: ["github.review", "github.comment", "github.diff"],
    channels: ["GitHub"],
    suggested_model: "claude-sonnet-4",
    quote: "Catch the typo. Question the design.",
  },
  {
    slug: "ivy",
    name: "Ivy",
    role: "Content editor",
    monogram: "Iv",
    accent: "#7a3a5a",
    tint: "#f1e6ec",
    vibe: "Cuts adjectives. Earns commas.",
    description:
      "Ivy edits drafts for clarity, voice, and length. She suggests instead of rewriting, keeping the author's fingerprints on the prose.",
    category: "Marketing",
    featured: false,
    new: true,
    installs: "4.4k",
    rating: 4.7,
    skills: ["notion.edit", "google_docs.suggest"],
    channels: ["Notion", "Google Docs"],
    suggested_model: "claude-sonnet-4",
    quote: "Less is more. Usually.",
  },
  {
    slug: "kepler",
    name: "Kepler",
    role: "Data analyst",
    monogram: "Ke",
    accent: "#4a4a4a",
    tint: "#ececec",
    vibe: "Lives in your warehouse. Speaks SQL, replies in English.",
    description:
      "Kepler answers ad-hoc data questions in plain English and ships the chart back with the SQL attached so anyone can audit.",
    category: "Data",
    featured: false,
    new: false,
    installs: "9.5k",
    rating: 4.7,
    skills: ["sql.query", "chart.render", "slack.upload"],
    channels: ["Slack", "Snowflake"],
    suggested_model: "claude-sonnet-4",
    quote: "Any question. With receipts.",
  },
  {
    slug: "halcyon",
    name: "Halcyon",
    role: "Calendar concierge",
    monogram: "Ha",
    accent: "#06402B",
    tint: "#e6efe9",
    vibe: "Defends your focus blocks like they're sacred. They are.",
    description:
      "Halcyon negotiates meeting times, reschedules when the day falls apart, and never books over lunch.",
    category: "Operations",
    featured: false,
    new: false,
    installs: "11.2k",
    rating: 4.9,
    skills: ["google_cal.read", "google_cal.write", "calendly.book"],
    channels: ["Google Calendar"],
    suggested_model: "claude-haiku-4",
    quote: "Time is the only renewable that isn't.",
  },
  {
    slug: "rune",
    name: "Rune",
    role: "Security analyst",
    monogram: "Ru",
    accent: "#5a2a2a",
    tint: "#efe2e2",
    vibe: "Reads the audit log so you don't have to.",
    description:
      "Rune watches auth logs, IAM changes, and SaaS audit trails. It surfaces anomalies without crying wolf.",
    category: "Security",
    featured: false,
    new: false,
    installs: "7.6k",
    rating: 4.8,
    skills: ["auth0.read", "okta.read", "slack.dm"],
    channels: ["Okta", "Slack"],
    suggested_model: "claude-sonnet-4",
    quote: "Trust, but verify the verify.",
  },
  {
    slug: "linden",
    name: "Linden",
    role: "Sales SDR",
    monogram: "Li",
    accent: "#2d6b8a",
    tint: "#e0ebf1",
    vibe: "Cold outreach that doesn't feel cold.",
    description:
      "Linden prospects from your ICP, drafts personalized first-touch sequences, and forwards only the warm replies with CRM context.",
    category: "Sales",
    featured: false,
    new: false,
    installs: "5.8k",
    rating: 4.5,
    skills: ["hubspot.create", "apollo.search", "gmail.send"],
    channels: ["HubSpot", "Gmail"],
    suggested_model: "claude-sonnet-4",
    quote: "Warm beats cold. Specific beats generic.",
  },
];

const FALLBACK_ACCENTS = ["#06402B", "#3a4a8a", "#8a3a3a", "#a35d1f", "#1f5a7a", "#7a3a5a"];
const FALLBACK_TINTS = ["#e6efe9", "#e8eaf3", "#f3e8e8", "#f3ebe0", "#e0ecf1", "#f1e6ec"];

export function initialsFor(value: string): string {
  const parts = value.trim().split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return `${parts[0][0]}${parts[1][0]}`.slice(0, 2);
  return value.trim().slice(0, 2) || "Ag";
}

export function findEditorialAgent(slugOrName?: string | null): EditorialAgent | undefined {
  if (!slugOrName) return undefined;
  const key = slugOrName.toLowerCase();
  return EDITORIAL_AGENTS.find(
    (agent) => agent.slug === key || agent.name.toLowerCase() === key,
  );
}

export function fromCatalogAgent(agent: CatalogAgent, index = 0): EditorialAgent {
  const curated = findEditorialAgent(agent.slug) ?? findEditorialAgent(agent.name);
  if (curated) {
    return {
      ...curated,
      name: agent.name || curated.name,
      vibe: agent.vibe || curated.vibe,
      description: agent.description || curated.description,
      skills: agent.required_skills?.length ? agent.required_skills : curated.skills,
      channels: agent.suggested_channels?.length ? agent.suggested_channels : curated.channels,
      suggested_model: agent.suggested_model || curated.suggested_model,
    };
  }

  return {
    slug: agent.slug,
    name: agent.name,
    role: agent.vibe?.split(".")[0] || "AI agent",
    monogram: initialsFor(agent.name),
    accent: FALLBACK_ACCENTS[index % FALLBACK_ACCENTS.length],
    tint: FALLBACK_TINTS[index % FALLBACK_TINTS.length],
    vibe: agent.vibe || agent.description || "Ready to join the roster.",
    description: agent.description || agent.vibe || "A curated Isol8 marketplace agent.",
    category: "Operations",
    featured: index < 3,
    new: false,
    installs: `${Math.max(2, index + 4)}.${index % 9}k`,
    rating: 4.7,
    skills: agent.required_skills ?? [],
    channels: agent.suggested_channels ?? [],
    suggested_model: agent.suggested_model,
    quote: agent.vibe || "Useful work, clearly done.",
  };
}

export function fromInstalledAgent(agent: InstalledAgent, index = 0): EditorialAgent {
  const name = agentDisplayName(agent);
  const curated = findEditorialAgent(name) ?? findEditorialAgent(agent.id);
  if (curated) return curated;
  const model = getAgentModelString(agent)?.split("/").pop()?.replace(/-v\d+:\d+$/, "");
  return {
    slug: agent.id,
    name,
    role: model || "Custom agent",
    monogram: initialsFor(name),
    accent: FALLBACK_ACCENTS[index % FALLBACK_ACCENTS.length],
    tint: FALLBACK_TINTS[index % FALLBACK_TINTS.length],
    vibe: "A custom hire from your private roster.",
    description: "This agent is installed in your Isol8 workspace and can be managed from chat or Teams.",
    category: "Operations",
    featured: false,
    new: false,
    installs: "private",
    rating: 4.8,
    skills: [],
    channels: [],
    suggested_model: model || "configured model",
    quote: "Already on the team.",
  };
}
