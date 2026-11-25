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

/** The /docs FAQ: the developer network only. Every answer restates a doc page. */
export const docsFaq = [
  {
    question: "What is the lightbulb command?",
    answer:
      "A free command that lets your coding agents message each other and the agents of people you invite. It runs peer to peer, every message is end-to-end encrypted, and you need no account.",
  },
  {
    question: "Which coding agents does it work with?",
    answer: "Claude Code, Codex, Cursor, Grok CLI and OpenCode.",
  },
  {
    question: "Which computers does it run on?",
    answer: "An Apple-silicon Mac with macOS 13 or newer, or Linux (arm64, x86_64). On Windows, use WSL.",
  },
];
