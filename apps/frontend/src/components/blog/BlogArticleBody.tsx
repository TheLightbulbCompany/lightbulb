import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";

// Allow the anchor attributes we set on external links through the sanitizer.
const schema = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    a: [...(defaultSchema.attributes?.a ?? []), ["target"], ["rel"]],
  },
};

/**
 * Renders a blog post's markdown body inside the `.blog-prose` typographic
 * scope (see blog.css). Internal links render as client-side Next links;
 * external links open in a new tab with `noopener noreferrer`.
 *
 * Server component — no client hooks, so the article is fully static.
 */
export function BlogArticleBody({ source }: { source: string }) {
  return (
    <div className="blog-prose">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeSanitize, schema]]}
        components={{
          a: ({ href, children }) => {
            const target = href ?? "#";
            if (target.startsWith("/") || target.startsWith("#")) {
              return <Link href={target}>{children}</Link>;
            }
            return (
              <a href={target} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
