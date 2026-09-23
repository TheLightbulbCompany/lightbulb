import { loadMarkdownDir } from "../markdown";

/** A doc is `src/content/docs/pages/<slug>.md`: JSON frontmatter + GFM body. */
export interface DocPage {
  slug: string;
  title: string;
  description: string;
  /** Sidebar position, 1-based. */
  order: number;
  content: string;
}

type Meta = Omit<DocPage, "slug" | "content">;

/** All docs in sidebar order. */
export function getAllDocs(): DocPage[] {
  return loadMarkdownDir<Meta>("docs/pages")
    .map(({ slug, meta, content }) => ({ slug, ...meta, content }))
    .sort((a, b) => a.order - b.order);
}

export function getDocBySlug(slug: string): DocPage | undefined {
  return getAllDocs().find((d) => d.slug === slug);
}

export const docsFaq = [
  {
    question: "What is Lightbulb used for?",
    answer:
      "Lightbulb is used to run an AI-native company: delegate work to agents, connect tools, manage goals and tasks, automate recurring workflows, and keep humans in the loop for sensitive actions.",
  },
  {
    question: "How does Lightbulb keep humans in control?",
    answer:
      "Agents ask for approval before governed actions such as production deploys, spend, refunds, legal or financial term changes, and other sensitive external actions. The same agent continues after approval.",
  },
  {
    question: "Where do I change model providers or plans?",
    answer:
      "Plans are managed in Settings. Model and provider choices are managed in the Model & Credits tab, including Lightbulb-managed credits, ChatGPT OAuth, and BYO API key paths.",
  },
  // Every answer below is verified against the product, not marketing copy:
  // prices are WORKSPACE_PRICE_USD / ULTRA_PRICE_USD in the backend settings,
  // the free tier is the `trial_local` path (a 30-day no-card window on the
  // server-selected free model), and the starter roster is
  // STARTER_ROSTER in paperclip_provisioning.py. There is no 14-day trial —
  // it was removed in July 2026. Keep this block true or delete the entry.
  {
    question: "Is Lightbulb free to try?",
    answer:
      "Yes. The free tier needs no credit card and runs for 30 days on the server-selected free model. The workspace sleeps when idle and wakes when you open it. Paid plans are Base at $20 a month, Workspace at $50 a month, and Ultra at $150 a month.",
  },
  {
    question: "How much does Lightbulb cost?",
    answer:
      "Workspace is $50 per month and Ultra is $150 per month. Both cover one workspace with unlimited members; Lightbulb does not charge per seat. Ultra provides a larger workspace and is required above 10 agents. A free tier with no credit card is available for evaluation.",
  },
  {
    question: "Which agents do I get when I sign up?",
    answer:
      "Every new workspace starts with six agents: a CEO plus Engineering, Finance, Growth, Analytics, and Customer Operations. The CEO is the front door and delegates to the specialists. You can rename any of them and hire more from the agent marketplace.",
  },
  {
    question: "Do Lightbulb agents remember work between sessions?",
    answer:
      "Yes. Each workspace runs in its own isolated cloud container with a persistent filesystem, so files, project state, and agent memory survive restarts and carry across sessions rather than resetting each conversation.",
  },
  {
    question: "Which AI models does Lightbulb run on?",
    answer:
      "Three provider paths. Lightbulb-managed is included with every plan and needs no setup — the free trial and Base run on DeepSeek within a daily allowance, and more models via prepaid credits are coming soon. Or connect your own ChatGPT account over OAuth, or bring your own Anthropic or OpenAI API key.",
  },
];
