"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/** Docs sidebar. Client-only for the active-page highlight; the item list is
 * read from disk by the server layout and passed down. */
export function DocsNav({ items }: { items: { slug: string; title: string }[] }) {
  const pathname = usePathname();
  const current = (href: string) => (pathname === href ? "page" : undefined);
  return (
    <nav className="docs-nav" aria-label="Documentation">
      <Link href="/docs" aria-current={current("/docs")}>Overview</Link>
      {items.map((d) => (
        <Link key={d.slug} href={`/docs/${d.slug}`} aria-current={current(`/docs/${d.slug}`)}>
          {d.title}
        </Link>
      ))}
    </nav>
  );
}
