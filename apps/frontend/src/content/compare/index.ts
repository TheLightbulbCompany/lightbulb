/**
 * The comparison pages (docs/marketing 11). One data file, one route: every
 * competitor cell carries the vendor page it came from and the day it was
 * read, and a test fails on a cell without both. Leo signs off every edit.
 *
 * Update rule: a cell we cannot reopen on the vendor's page becomes "Not
 * published"; a page's `checkedOn` moves only when every cell was reopened.
 */

import { teamMonthlyUsd, type Pricing } from "@/lib/pricing";

export type RowKey = "who" | "where" | "sees" | "models" | "price" | "approvals" | "offline" | "work";

export const ROWS: { key: RowKey; label: string }[] = [
  { key: "who", label: "Who it's for" },
  { key: "where", label: "Where the agent lives" },
  { key: "sees", label: "Who sees the work" },
  { key: "models", label: "Any model" },
  { key: "price", label: "Price for 8 people" },
  { key: "approvals", label: "Approvals and record" },
  { key: "offline", label: "Works while nobody is online" },
  { key: "work", label: "Code and non-code work" },
];

export interface Cell {
  text: string;
  /** The page the fact was read on. */
  source: string;
  /** ISO date the source was opened. */
  checkedOn: string;
}

export interface Column {
  name: string;
  cells: Record<RowKey, Cell>;
}

export interface ComparePage {
  slug: string;
  /** Public path. */
  path: string;
  title: string;
  description: string;
  h1: string;
  intro: string;
  /** Competitor columns; the Lightbulb column is added by the page. */
  columns: Column[];
  betterTitle: string;
  better: string[];
  faq: { q: string; a: string }[];
  /** A price question, answered only when the billing API gave a price. */
  priceFaq?: (p: Pricing) => { q: string; a: string };
}

export const COMPARE_AUTHOR = "Leo";

const CHECKED = "2026-10-13";
const OURS = "2026-10-14";
const c = (text: string, source: string, checkedOn = CHECKED): Cell => ({ text, source, checkedOn });

const SITE = "https://thelightbulb.company";

/** The Lightbulb column: the same on every page. */
export const LIGHTBULB: Column = {
  name: "Lightbulb",
  cells: {
    who: c("Founder-led teams of 3–15 already running AI agents", `${SITE}/request-setup`, OURS),
    where: c("A member of your channels, running in your workspace's own cloud", `${SITE}/security`, OURS),
    sees: c("Everyone in the room; one shared record", `${SITE}/docs/get-started`, OURS),
    models: c("Lightbulb-managed or your ChatGPT account", `${SITE}/security`, OURS),
    // Filled from the billing API by lightbulbColumn(); this is the copy when the API cannot be read.
    price: c("One price for the workspace; see pricing", `${SITE}/pricing`, OURS),
    approvals: c("Approval cards for plans, spending and new hires; who did what and who approved it", `${SITE}/docs/approvals-security-privacy`, OURS),
    offline: c("Yes: routines, email and a nightly CEO run", `${SITE}/docs/skills-and-automations`, OURS),
    work: c("Both: shared Claude Code and Codex sessions, plus email, books and the task board", `${SITE}/docs/get-started`, OURS),
  },
};

const SLACK_PRICING = "https://slack.com/pricing";
const SLACKBOT = "https://slack.com/features/slackbot";
const SLACK_AGENTS = "https://slack.com/ai-agents";
const MS = "https://www.microsoft.com/en-us/microsoft-365/copilot/business";
const GROK = "https://docs.x.ai/grok-bot";
const GROK_TEAMS = "https://docs.x.ai/grok-bot/teams-and-enterprises";
const GROK_FAQ = "https://docs.x.ai/grok-bot/faq";
const GROK_SEC = "https://docs.x.ai/grok-bot/security-faq";
const CURSOR_PRICING = "https://cursor.com/pricing";
const BLOOME = "https://bloome.im";
const AMP = "https://finviz.com/news/399638/atlassian-introduces-amp-the-agentic-multiplayer-protocol-to-power-human-ai-collaboration";

const SLACK: Column = {
  name: "Slack Business+ with Slackbot",
  cells: {
    who: c("Every company size", SLACK_PRICING),
    where: c("Slackbot, a personal AI in Slack; third-party agent apps", SLACKBOT),
    sees: c("The person who asked; answers can be shared to a channel", SLACKBOT),
    models: c("Not published", SLACKBOT),
    price: c("$120/month annual ($15 × 8); Agentforce price not published", SLACK_PRICING),
    approvals: c("Not published for Slackbot", SLACKBOT),
    offline: c("Not published", SLACKBOT),
    work: c("Non-code, through connected apps", SLACKBOT),
  },
};

const SLACK_SHORT: Column = {
  name: "Slack + Slackbot",
  cells: {
    who: c("Every company", SLACK_PRICING),
    where: c("Personal AI in Slack", SLACKBOT),
    sees: c("The person who asked", SLACKBOT),
    models: c("Not published", SLACKBOT),
    price: c("See Slack's page", SLACK_PRICING),
    approvals: c("Not published", SLACKBOT),
    offline: c("Not published", SLACKBOT),
    work: c("Non-code", SLACK_AGENTS),
  },
};

const TEAMS_COPILOT: Column = {
  name: "Microsoft 365 Business Standard with Copilot",
  cells: {
    who: c("Microsoft 365 customers, up to 300 users", MS),
    where: c("Copilot in Microsoft 365 apps; agents via Copilot Studio", MS),
    sees: c("Your Microsoft 365 tenant", MS),
    models: c("“LLM model choice”", MS),
    price: c("$188/month paid yearly ($23.50 × 8)", MS),
    approvals: c("Not published on the pricing page", MS),
    offline: c("Not published", MS),
    work: c("Mostly documents, email and meetings", MS),
  },
};

const GROK_BOT: Column = {
  name: "Grok Bot",
  cells: {
    who: c("Individuals and Cursor Teams/Enterprise", GROK_TEAMS),
    where: c("One cloud computer per person, shared by that person's Bots", GROK_TEAMS),
    sees: c("That person; templates share setup, not history", GROK_FAQ),
    models: c("Cursor manages model selection", GROK_TEAMS),
    price: c("Included in Cursor Teams, $30+/user/month: $240+", CURSOR_PRICING),
    approvals: c("Auto-review can ask first; audit logs are Enterprise only", GROK_SEC),
    offline: c("Yes", GROK),
    work: c("Both", GROK),
  },
};

const BLOOME_COL: Column = {
  name: "Bloome",
  cells: {
    who: c("Teams of people and agents", BLOOME),
    where: c("Group chat; sandbox per task", BLOOME),
    sees: c("Chat members", BLOOME),
    models: c("Many models and coding agents", BLOOME),
    price: c("“Free to try”; no tiers published", BLOOME),
    approvals: c("Audit trail; per-tool permissions", BLOOME),
    offline: c("Not published", BLOOME),
    work: c("Both", BLOOME),
  },
};

export const COMPARE_PAGES: ComparePage[] = [
  {
    slug: "slack",
    path: "/compare/slack",
    title: "Lightbulb vs Slack AI and Slackbot | Lightbulb",
    description:
      "How Lightbulb and Slack's Slackbot differ for small teams using AI agents: who sees the work, which models, approvals, and price for 8 people.",
    h1: "Lightbulb vs Slack AI: an assistant for each person, or AI employees for the team",
    intro:
      "Slack is where most teams already talk, and Slackbot now answers questions and takes actions across your connected apps. Lightbulb is a team workspace where AI employees are members of your channels, with their own tools, memory, email and approval rules. If you are choosing where your people and your agents should work together, this page compares the two plainly.",
    columns: [SLACK],
    betterTitle: "What Slack does better",
    better: [
      "Slack is the place your team already talks, with thousands of apps, Slack Connect with other companies, huddles and years of polish. If your team is larger than 15 or works with many outside partners, Slack will serve the conversation better than we will today.",
      "Slackbot is included in Business+ and knows your whole Slack history from day one. Lightbulb's agents know what happens in Lightbulb and in the tools you connect; they do not read your Slack.",
      "Slack has enterprise controls, mobile apps in every store and a long security record. We are early: the iPhone app is in TestFlight and SSO is not shipped. Slack added agents to chat. Lightbulb built the company around them.",
    ],
    faq: [
      {
        q: "Can we keep Slack?",
        a: "Yes. Many teams keep Slack for talk and run their AI employees in Lightbulb. Your team joins the rooms where agents are working.",
      },
      {
        q: "Is Slackbot an AI employee?",
        a: "Slackbot is a personal assistant; its interactions are private to you. In Lightbulb the agents are shared members with their own email, tools and approval rules.",
      },
    ],
    priceFaq: (p) => ({
      q: "What does it cost for 8 people?",
      a: `Slack Business+ is $120 a month billed yearly. Lightbulb is $${teamMonthlyUsd(p, 8)} a month for the workspace, plus AI usage.`,
    }),
  },
  {
    slug: "slack-alternatives-for-ai-agents",
    path: "/compare/slack-alternatives-for-ai-agents",
    title: "Slack alternatives for teams that use AI agents (2026) | Lightbulb",
    description:
      "Six options for a small team that wants AI agents in its chat: Slack itself, Microsoft Teams, Lightbulb, Bloome, Grok Bot and Atlassian. Who each fits.",
    h1: "Alternatives to Slack for teams that use AI agents",
    intro:
      "If your team runs AI agents, the question is no longer only where people talk. It is where the agents work, who can see what they did, and who approves it. Here are six honest options, Slack included, with what each does well. We make Lightbulb, so we put it first and say where others fit better.",
    columns: [
      SLACK_SHORT,
      {
        name: "Teams + Copilot",
        cells: {
          who: c("Microsoft 365 customers", MS),
          where: c("Microsoft 365 apps", MS),
          sees: c("Your tenant", MS),
          models: c("“LLM model choice”", MS),
          price: c("$188", MS),
          approvals: c("Not published", MS),
          offline: c("Not published", MS),
          work: c("Mostly non-code", MS),
        },
      },
      { name: "Bloome", cells: { ...BLOOME_COL.cells, where: c("Group chat", BLOOME), models: c("Yes", BLOOME), price: c("Not published", BLOOME), approvals: c("Audit trail", BLOOME) } },
      {
        name: "Grok Bot",
        cells: {
          who: c("Cursor users", GROK_TEAMS),
          where: c("One cloud computer per person", GROK_TEAMS),
          sees: c("That person", GROK_FAQ),
          models: c("Cursor picks", GROK_TEAMS),
          price: c("$240+ (Cursor Teams)", CURSOR_PRICING),
          approvals: c("Auto-review; audit logs on Enterprise", GROK_SEC),
          offline: c("Yes", GROK),
          work: c("Both", GROK),
        },
      },
      {
        name: "Atlassian AMP",
        cells: {
          who: c("Jira and Confluence companies", AMP),
          where: c("Jira, Confluence, Loom", AMP),
          sees: c("Your Atlassian workspace", AMP),
          models: c("Not announced", AMP),
          price: c("Not announced", AMP),
          approvals: c("Full audit trail", AMP),
          offline: c("Not announced", AMP),
          work: c("Both", AMP),
        },
      },
    ],
    betterTitle: "What the others do better",
    better: [
      "Slack and Teams have the deepest app catalogues and the security reviews large customers ask for. If your company lives in Microsoft 365, Copilot sits in Word, Excel and Outlook, where Lightbulb does not. Atlassian's agents can run as a named user with a full audit trail, inside the tools Jira teams already use. Grok Bot gives each person a capable cloud computer that keeps working with the laptop closed.",
    ],
    faq: [
      {
        q: "What is the best Slack alternative for a small team with AI agents?",
        a: "If the agents should be shared members of the team, Lightbulb or Bloome. If you only want a personal assistant in chat, Slackbot or Copilot.",
      },
      { q: "Do we have to leave Slack?", a: "No. Keep Slack for talk and add Lightbulb where agents work." },
    ],
    priceFaq: (p) => ({
      q: "Which is cheapest for 8 people?",
      a: `Compare the Price row. Lightbulb is $${teamMonthlyUsd(p, 8)} a month; Bloome has not published a price.`,
    }),
  },
  {
    slug: "grok-bot",
    path: "/compare/grok-bot",
    title: "Lightbulb vs Grok Bot: AI for each person, or for the team | Lightbulb",
    description:
      "Grok Bot gives each person a cloud computer and a set of Bots. Lightbulb gives the team one set of AI employees everyone can see. An honest comparison.",
    h1: "Lightbulb vs Grok Bot",
    intro:
      "Grok Bot, from xAI and Cursor, gives each person their own Bots on a persistent cloud computer, with routines that keep running when the laptop is closed. Lightbulb gives a team one set of AI employees in shared channels, with one record and approval rules. Both do real work in real tools. The difference is who the agents belong to.",
    columns: [GROK_BOT],
    betterTitle: "What Grok Bot does better",
    better: [
      "Each person gets a full computer with a browser, terminal and files, on desktop and mobile apps, and it keeps working while they are away. Admin controls are deep: network policy, team rules, team secrets and SIEM export on Enterprise.",
      "If your team already pays for Cursor, Grok Bot costs nothing extra. Routines that nobody reads pause themselves, a rule we took. Grok Bot gives each person a team of Bots. Lightbulb gives the team one set of AI employees everyone can see.",
    ],
    faq: [
      {
        q: "Can a teammate see my Grok Bot's work?",
        a: "Its docs say users cannot reach each other's computers, and a shared template does not include conversation history. In Lightbulb the room sees the work.",
      },
      {
        q: "Which models?",
        a: "Grok Bot's docs say Cursor manages model selection. Lightbulb runs Lightbulb-managed models or uses your ChatGPT account.",
      },
      { q: "Can we use both?", a: "Yes. Many teams use a personal agent and a shared workspace." },
    ],
  },
  {
    slug: "microsoft-teams-copilot",
    path: "/compare/microsoft-teams-copilot",
    title: "Lightbulb vs Microsoft Teams with Copilot | Lightbulb",
    description:
      "Microsoft 365 with Copilot vs Lightbulb for a small team using AI agents: where agents live, who sees their work, approvals, and price for 8 people.",
    h1: "Lightbulb vs Microsoft Teams with Copilot",
    intro:
      "Microsoft Teams with Copilot brings AI into Word, Excel, Outlook and Teams, with prebuilt agents and Copilot Studio for building your own. Lightbulb is a team workspace where AI employees are members of your channels and do work the whole team can see and approve. If your company does not live in Microsoft 365, the choice is simpler than it looks.",
    columns: [TEAMS_COPILOT],
    betterTitle: "What Microsoft does better",
    better: [
      "Copilot works inside the documents, spreadsheets and email your team already uses, and the price includes Office apps, email hosting and 1 TB of storage per person. For a company on Microsoft 365, nothing else is that close to the work.",
      "Microsoft has the identity, compliance and admin tooling large buyers require. Lightbulb does not have SSO yet.",
    ],
    faq: [
      {
        q: "Does Copilot work outside Microsoft 365?",
        a: "Copilot Business requires an eligible Microsoft 365 plan. Lightbulb connects to the tools you use, Google or Microsoft.",
      },
      { q: "Can we use both?", a: "Yes. Lightbulb agents can work alongside a Microsoft 365 company." },
    ],
    priceFaq: (p) => ({
      q: "What does it cost for 8 people?",
      a: `$188 a month for Business Standard with Copilot, paid yearly. Lightbulb is $${teamMonthlyUsd(p, 8)} a month plus AI usage.`,
    }),
  },
  {
    slug: "bloome",
    path: "/compare/bloome",
    title: "Lightbulb vs Bloome | Lightbulb",
    description:
      "Bloome and Lightbulb both put people and AI agents in one chat. Where each fits, with every cell sourced and dated.",
    h1: "Lightbulb vs Bloome",
    intro:
      "Bloome is a group chat “where people and agents work together”, with @mentions, many models and coding agents, and an audit trail. Lightbulb is close in shape. This page says where each fits.",
    columns: [BLOOME_COL],
    betterTitle: "What Bloome does better",
    better: [
      "Bloome runs many models and coding agents, including Claude Code, Codex, Gemini CLI and OpenCode, in one group chat, and it is free to try. If you want the widest choice of coding agents in a chat, start there.",
      "Lightbulb fits when the agents are shared employees of the company, with their own email, tools and approval rules, and when the same team also hands off email, books and approvals.",
    ],
    faq: [
      {
        q: "Are Bloome and Lightbulb the same kind of product?",
        a: "Close. Both put people and agents in one chat with @mentions. Lightbulb's agents are members of the company with their own email, tools and approval rules.",
      },
      { q: "What does Bloome cost?", a: "Bloome says “Free to try” and has not published tiers." },
    ],
  },
];

/** The Lightbulb column with its price cell read from the billing API (null: neutral copy). */
export function lightbulbColumn(p: Pricing | null): Column {
  if (!p) return LIGHTBULB;
  const text =
    `$${teamMonthlyUsd(p, 8)} a month ($${p.teamPriceUsd} for up to ${p.includedMembers} people, ` +
    `$${p.seatPriceUsd} each after), plus AI usage`;
  return { ...LIGHTBULB, cells: { ...LIGHTBULB.cells, price: { ...LIGHTBULB.cells.price, text } } };
}

/** The page's questions, with its price question when the price is known. */
export function pageFaq(page: ComparePage, p: Pricing | null): { q: string; a: string }[] {
  return p && page.priceFaq ? [...page.faq, page.priceFaq(p)] : page.faq;
}

/** The oldest date any competitor cell was read: what "Checked on" may claim. */
export function checkedOn(page: ComparePage): string {
  return page.columns
    .flatMap((col) => Object.values(col.cells).map((cell) => cell.checkedOn))
    .sort()[0];
}

export function getComparePage(slug: string): ComparePage | undefined {
  return COMPARE_PAGES.find((p) => p.slug === slug);
}
