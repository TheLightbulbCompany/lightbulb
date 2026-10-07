import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize from "rehype-sanitize";

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
        rehypePlugins={[rehypeSanitize]}
        components={{
          a: ({ href, children }) => {
            const target = href ?? "#";
            // Internal = app-relative path or in-page anchor. Exclude
            // protocol-relative URLs ("//host"), which are external.
            const isInternal =
              (target.startsWith("/") && !target.startsWith("//")) ||
              target.startsWith("#");
            if (isInternal) {
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
