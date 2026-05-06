import ReactMarkdown from "react-markdown";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";

const schema = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    a: [
      ...(defaultSchema.attributes?.a ?? []),
      ["target"],
      ["rel"],
    ],
  },
};

/**
 * Renders seller-supplied markdown safely on the listing detail page.
 * No raw HTML; allowlist via rehype-sanitize. Links open in a new tab
 * with `noopener noreferrer` to avoid window-opener leaks.
 */
export function MarkdownDescription({ source }: { source: string }) {
  return (
    <div className="prose prose-stone max-w-none font-[var(--font-dm)] text-[var(--ink-soft)] [&_h1]:font-editorial-serif [&_h2]:font-editorial-serif [&_h3]:font-editorial-serif [&_a]:text-[var(--forest)] [&_code]:bg-[var(--cream-deeper)] [&_code]:text-[var(--forest)] [&_code]:rounded [&_code]:px-1 [&_code]:py-0.5">
      <ReactMarkdown
        rehypePlugins={[[rehypeSanitize, schema]]}
        components={{
          a: ({ href, children, ...rest }) => (
            <a
              {...rest}
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="underline"
            >
              {children}
            </a>
          ),
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
