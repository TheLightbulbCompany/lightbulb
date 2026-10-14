import Link from "next/link";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { COMPARE_AUTHOR, LIGHTBULB, ROWS, checkedOn, type ComparePage } from "@/content/compare";
import { JsonLd, breadcrumbJsonLd, pageMetadata } from "@/lib/seo";

export function compareMetadata(page: ComparePage) {
  return pageMetadata({ title: page.title, description: page.description, path: page.path });
}

export function ComparePageView({ page }: { page: ComparePage }) {
  const columns = [LIGHTBULB, ...page.columns];
  const source = `compare-${page.slug}`;
  return (
    <main className="ld-page min-h-screen">
      <JsonLd
        data={breadcrumbJsonLd([
          { name: "Home", path: "/" },
          { name: "Compare", path: "/compare" },
          { name: page.h1, path: page.path },
        ])}
      />
      <JsonLd
        data={{
          "@context": "https://schema.org",
          "@type": "FAQPage",
          mainEntity: page.faq.map(({ q, a }) => ({
            "@type": "Question",
            name: q,
            acceptedAnswer: { "@type": "Answer", text: a },
          })),
        }}
      />
      <Navbar />
      <article className="mx-auto max-w-6xl px-6 py-20 md:py-28">
        <p className="ld-mono text-xs uppercase tracking-[0.18em] text-[var(--midnight-muted)]">
          Compare · By {COMPARE_AUTHOR}
        </p>
        <h1 className="mt-5 font-editorial-serif text-4xl leading-tight text-[var(--midnight)] md:text-6xl">{page.h1}</h1>
        <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--midnight-soft)]">{page.intro}</p>

        <div className="mt-12 overflow-x-auto">
          <table className="w-full min-w-[640px] border-collapse text-left text-sm leading-6">
            <thead>
              <tr>
                <th scope="col" className="border-b border-[var(--rule)] py-3 pr-4" />
                {columns.map((col) => (
                  <th key={col.name} scope="col" className="border-b border-[var(--rule)] py-3 pr-4 font-semibold text-[var(--midnight)]">
                    {col.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ROWS.map((row) => (
                <tr key={row.key}>
                  <th scope="row" className="border-b border-[var(--rule)] py-3 pr-4 align-top font-medium text-[var(--midnight)]">
                    {row.label}
                  </th>
                  {columns.map((col) => (
                    <td key={col.name} className="border-b border-[var(--rule)] py-3 pr-4 align-top text-[var(--midnight-soft)]">
                      <a href={col.cells[row.key].source} rel="noopener" className="hover:underline">
                        {col.cells[row.key].text}
                      </a>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-sm text-[var(--midnight-muted)]">
          Checked on {checkedOn(page)}. Every cell links to the page it came from.
        </p>

        <section className="mt-14 max-w-3xl">
          <h2 className="font-editorial-serif text-3xl text-[var(--midnight)]">{page.betterTitle}</h2>
          {page.better.map((p) => (
            <p key={p.slice(0, 40)} className="mt-4 text-lg leading-8 text-[var(--midnight-soft)]">
              {p}
            </p>
          ))}
        </section>

        <section className="mt-14 max-w-3xl">
          <h2 className="font-editorial-serif text-3xl text-[var(--midnight)]">Questions</h2>
          <dl className="mt-4 space-y-6">
            {page.faq.map(({ q, a }) => (
              <div key={q}>
                <dt className="font-semibold text-[var(--midnight)]">{q}</dt>
                <dd className="mt-2 leading-7 text-[var(--midnight-soft)]">{a}</dd>
              </div>
            ))}
          </dl>
        </section>

        <div className="mt-14 flex flex-wrap gap-4">
          <Link href={`/request-setup?source=${source}`} className="lb-btn lb-btn-p lb-btn-xl">
            Request a guided setup
          </Link>
          <Link href={`/sign-up?source=${source}`} className="lb-btn lb-btn-s lb-btn-xl">
            Start free
          </Link>
        </div>
      </article>
      <Footer />
    </main>
  );
}
