"use client";

import { Fragment } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

type Item = { slug: string; title: string; section?: string };

/** Docs sidebar. Client-only for the active-page highlight; the item list is
 * read from disk by the server layout and passed down in order. A `section`
 * heading is printed where a new section starts. */
export function DocsNav({ items }: { items: Item[] }) {
  const pathname = usePathname();
  const current = (href: string) => (pathname === href ? "page" : undefined);
  return (
    <nav className="docs-nav" aria-label="Documentation">
      <Link href="/docs" aria-current={current("/docs")}>Overview</Link>
      {items.map((d, i) => (
        <Fragment key={d.slug}>
          {d.section && d.section !== items[i - 1]?.section && (
            <span className="docs-nav-section">{d.section}</span>
          )}
          <Link href={`/docs/${d.slug}`} aria-current={current(`/docs/${d.slug}`)}>
            {d.title}
          </Link>
        </Fragment>
      ))}
    </nav>
  );
}
