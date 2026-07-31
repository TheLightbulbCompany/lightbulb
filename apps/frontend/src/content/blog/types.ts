/**
 * Blog content model.
 *
 * Posts are authored as plain TypeScript modules (see `./posts/*`) and
 * collected in `./index.ts`. This keeps the content type-checked, statically
 * bundled (no runtime filesystem reads), and free of extra markdown-tooling
 * dependencies — the body is rendered with the react-markdown stack already
 * used elsewhere in the app.
 */
export interface BlogPost {
  /** URL segment: /blog/<slug>. Kebab-case, stable once published. */
  slug: string;
  /** Post title — used as <h1>, <title>, and OpenGraph title. */
  title: string;
  /** One-to-two sentence summary — meta description + index card excerpt. */
  description: string;
  /** Publish date, ISO `YYYY-MM-DD`. Drives sort order + sitemap lastmod. */
  date: string;
  /** Display author name. */
  author: string;
  /** Lowercase topic tags for grouping / future filtering. */
  tags: string[];
  /** Rough read time in minutes, shown on the index + article header. */
  readingMinutes: number;
  /** Markdown body (GFM). Rendered via react-markdown + rehype-sanitize. */
  content: string;
}
