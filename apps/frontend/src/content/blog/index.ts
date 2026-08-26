import { loadMarkdownDir, readingMinutes } from "../markdown";

/** A post is `src/content/blog/posts/<slug>.md`: JSON frontmatter + GFM body. */
export interface BlogPost {
  slug: string;
  title: string;
  description: string;
  /** ISO `YYYY-MM-DD`. Drives sort order + sitemap lastmod. */
  date: string;
  author: string;
  tags: string[];
  readingMinutes: number;
  /** Markdown body. Rendered via react-markdown + rehype-sanitize. */
  content: string;
}

type Meta = Omit<BlogPost, "slug" | "readingMinutes" | "content">;

/** All posts, newest first. */
export function getAllPosts(): BlogPost[] {
  return loadMarkdownDir<Meta>("blog/posts")
    .map(({ slug, meta, content }) => ({ slug, ...meta, readingMinutes: readingMinutes(content), content }))
    .sort((a, b) => b.date.localeCompare(a.date));
}

export function getPostBySlug(slug: string): BlogPost | undefined {
  return getAllPosts().find((p) => p.slug === slug);
}
