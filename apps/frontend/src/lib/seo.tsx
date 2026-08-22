import type { Metadata } from "next";

export const SITE_URL = "https://isol8.co";
export const SITE_NAME = "isol8";
export const OG_IMAGE = "/og-image.png";

export function absoluteUrl(path = "/") {
  return new URL(path, SITE_URL).toString();
}

export function pageMetadata({
  title,
  description,
  path,
}: {
  title: string;
  description: string;
  path: string;
}): Metadata {
  const url = absoluteUrl(path);
  return {
    title,
    description,
    alternates: { canonical: url },
    openGraph: {
      title,
      description,
      url,
      siteName: SITE_NAME,
      type: "website",
      images: [{ url: OG_IMAGE, width: 1200, height: 630, alt: SITE_NAME }],
    },
    twitter: {
      card: "summary_large_image",
      title,
      description,
      images: [OG_IMAGE],
    },
  };
}

export function JsonLd({ data }: { data: unknown }) {
  return (
    <script
      type="application/ld+json"
      dangerouslySetInnerHTML={{ __html: JSON.stringify(data).replace(/</g, "\\u003c") }}
    />
  );
}

export const organizationJsonLd = {
  "@context": "https://schema.org",
  "@type": "Organization",
  name: "isol8",
  url: SITE_URL,
  logo: absoluteUrl("/icon.png"),
  sameAs: ["https://www.linkedin.com/company/isol8ai/"],
};

export const websiteJsonLd = {
  "@context": "https://schema.org",
  "@type": "WebSite",
  name: SITE_NAME,
  url: SITE_URL,
  description:
    "isol8 gives companies persistent AI agents, an agent workspace, and an agent marketplace for autonomous operations.",
};

export const softwareApplicationJsonLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: "isol8",
  applicationCategory: "BusinessApplication",
  operatingSystem: "Web",
  url: SITE_URL,
  description:
    "An AI agent workspace for running company operations with persistent, cloud-isolated agents.",
  offers: {
    "@type": "Offer",
    price: "0",
    priceCurrency: "USD",
  },
};

export function breadcrumbJsonLd(items: { name: string; path: string }[]) {
  return {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: items.map((item, index) => ({
      "@type": "ListItem",
      position: index + 1,
      name: item.name,
      item: absoluteUrl(item.path),
    })),
  };
}

export const seoLandingPages = [
  ["ai-agent-workspace", "AI Agent Workspace for Company Operations | isol8", "An AI agent workspace for real company work", "Give AI agents a persistent workspace with tools, memory, approvals, and isolated execution so they can run operations end to end.", "Agent workspace", "Start with an agent workspace that keeps work, context, approvals, and execution together instead of scattering them across chat threads.", ["Persistent agent workspaces", "Tool access with oversight", "Company memory and task handoffs"]],
  ["ai-agent-marketplace", "AI Agent Marketplace for Business Operations | isol8", "Find deployable AI agents for the work your company already does", "Browse and install AI agents for support, operations, research, sales, and internal workflows from the isol8 marketplace.", "Agent marketplace", "The isol8 marketplace helps teams hire specialized agents that arrive with the skills and tools needed for common business workflows.", ["Curated operational agents", "One-click deployment", "Agent profiles built for work, not demos"]],
  ["autonomous-ai-company", "Autonomous AI Company Platform | isol8", "Turn repeated workflows into an autonomous AI company", "isol8 coordinates AI agents, approvals, memory, and execution loops so companies can automate high-context operations safely.", "Autonomous company", "Use isol8 to move from one-off prompts to accountable agents that can pick up tasks, coordinate, and report durable progress.", ["Event-driven work intake", "Human approval checkpoints", "Durable issue and run history"]],
  ["persistent-ai-agents", "Persistent AI Agents That Keep Working | isol8", "Persistent AI agents with memory, tools, and accountability", "Run AI agents that remember company context, continue work across sessions, and leave inspectable evidence for every task.", "Persistent agents", "isol8 agents are designed for long-lived work: they wake on tasks, use connected tools, document progress, and hand back proof.", ["Cross-session memory", "Run logs and work products", "Task-based wakeups"]],
  ["ai-agents-for-slack", "AI Agents for Slack Workflows | isol8", "Bring AI agents into Slack-driven operations", "Use isol8 to connect agent work with Slack-heavy teams, routing tasks and updates into governed operational workflows.", "Slack workflows", "For teams that live in Slack, isol8 provides the agent workspace behind the scenes: task intake, execution, approvals, and status.", ["Slack-aware task routing", "Agent status updates", "Approval-friendly collaboration"]],
  ["ai-agents-for-discord", "AI Agents for Discord Communities and Teams | isol8", "AI agents for Discord communities, teams, and support loops", "Coordinate AI agents around Discord-originated work while keeping execution, memory, and approvals in isol8.", "Discord workflows", "isol8 gives Discord-first teams a place for agents to do real follow-through after conversations identify work to be done.", ["Community operations", "Support triage", "Durable follow-up outside chat"]],
  ["ai-agents-for-telegram", "AI Agents for Telegram Operations | isol8", "AI agents for Telegram-first teams", "Route Telegram-originated requests into persistent AI agents that can execute, report, and ask for approvals through isol8.", "Telegram workflows", "When work starts in Telegram, isol8 can provide the governed workspace where agents carry that work to completion.", ["Fast task capture", "Persistent execution", "Auditable agent updates"]],
  ["ai-chief-of-staff", "AI Chief of Staff for Operating Cadence | isol8", "An AI chief of staff for tasks, follow-through, and operating rhythm", "Deploy AI chief-of-staff agents to track priorities, coordinate handoffs, summarize progress, and keep operations moving.", "Chief of staff agent", "A chief-of-staff agent in isol8 can monitor work queues, prepare updates, file follow-ups, and keep priorities visible.", ["Operating cadence support", "Follow-up tracking", "Progress summaries"]],
  ["ai-operations-agent", "AI Operations Agent for Repeated Business Work | isol8", "AI operations agents that keep business workflows moving", "Use isol8 operations agents to triage tasks, coordinate systems, document outcomes, and handle repeatable company workflows.", "Operations agent", "Operations agents are useful when the work crosses tools and needs judgment, audit trails, and occasional human decisions.", ["Workflow triage", "System coordination", "Evidence-backed completion"]],
  ["ai-customer-support-agent", "AI Customer Support Agent for Triage and Resolution | isol8", "AI customer support agents for triage, answers, and follow-up", "Deploy AI support agents that understand company context, route escalations, draft responses, and leave a record of customer work.", "Support agent", "isol8 support agents can help handle inbound work while preserving escalation paths, evidence, and reviewable outcomes.", ["Inbound triage", "Drafted responses", "Escalation and resolution records"]],
].map(([slug, title, h1, description, kicker, primary, bullets]) => ({ slug, title, h1, description, kicker, primary, bullets })) as {
  slug: string;
  title: string;
  h1: string;
  description: string;
  kicker: string;
  primary: string;
  bullets: string[];
}[];

export type SeoLandingPage = (typeof seoLandingPages)[number];
