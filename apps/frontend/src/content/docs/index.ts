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
    question: "What is Lightbulb AI used for?",
    answer:
      "Lightbulb AI is used to run an AI-native company: delegate work to agents, connect tools, manage goals and tasks, automate recurring workflows, and keep humans in the loop for sensitive actions.",
  },
  {
    question: "Can Lightbulb AI connect to tools that do not have native integrations?",
    answer:
      "Yes. Native connectors and webhooks are preferred, but agents can also use the Browser panel to operate login-bound or UI-only tools when a browser workflow is the practical integration path.",
  },
  {
    question: "How does Lightbulb AI keep humans in control?",
    answer:
      "Agents ask for approval before governed actions such as production deploys, spend, refunds, legal or financial term changes, and other sensitive external actions. The same agent continues after approval.",
  },
  {
    question: "Where do I change model providers or plans?",
    answer:
      "Plans are managed in Settings. Model and provider choices are managed in the Model & Credits tab, including Lightbulb AI-managed credits, ChatGPT OAuth, and BYO API key paths.",
  },
];
