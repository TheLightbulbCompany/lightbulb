import { loadMarkdownDir } from "../markdown";

/** A doc is `src/content/docs/pages/<slug>.md`: JSON frontmatter + GFM body. */
export interface DocPage {
  slug: string;
  title: string;
  description: string;
  /** Sidebar position, 1-based. */
  order: number;
  /** Optional sidebar group heading, printed where the group starts. */
  section?: string;
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
      "Manage your plan in Settings → Billing. During workspace setup, choose Lightbulb-managed inference or sign in with ChatGPT. Existing workspaces that were already configured with a saved API key can keep using it.",
  },
  // Every answer below is verified against the product, not marketing copy:
  // prices are WORKSPACE_PRICE_USD / ULTRA_PRICE_USD in the backend settings,
  // the free tier is the `trial_local` path (a 30-day no-card window on the
  // server-selected free model), and the starter roster is
  // STARTER_ROSTER in workspace_provisioning.py. There is no 14-day trial —
  // it was removed in July 2026. Keep this block true or delete the entry.
  {
    question: "Is Lightbulb free to try?",
    answer:
      "Yes. The free tier needs no credit card and runs for 30 days on the server-selected free model. The workspace sleeps when idle and wakes when you open it. After that there is one plan; see /pricing.",
  },
  {
    question: "How much does Lightbulb cost?",
    answer:
      "One plan: a flat monthly price for the workspace and its first people, then a per-person price for each member after. Agents' AI runs on Lightbulb-managed inference, funded with prepaid credits, or your ChatGPT account, billed by OpenAI. Current prices are on /pricing.",
  },
  {
    question: "Which agents do I get when I sign up?",
    answer:
      "Every new workspace starts with six agents: a CEO plus Engineering, Finance, Growth, Analytics, and Customer Operations. The CEO is the front door and delegates to the specialists. You can rename any of them and hire more from the agent marketplace.",
  },
  {
    question: "Do Lightbulb agents remember work between sessions?",
    answer:
      "Yes. On the free network, agents save facts with remember and read them with recall, and memory is kept on your computers. A workspace also has a long-term memory every agent shares, stored in AWS (Bedrock AgentCore) in our account, and its files and project state survive restarts. What goes where is on /docs/memory.",
  },
  {
    question: "Which AI models does Lightbulb run on?",
    answer:
      "Choose Lightbulb-managed inference, which needs no setup and runs on DeepSeek within the free trial's daily allowance, or sign in with your ChatGPT account. Paid workspaces can add prepaid credits for more managed models.",
  },
];
