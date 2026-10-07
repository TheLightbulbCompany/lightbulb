import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import {
  JsonLd,
  absoluteUrl,
  breadcrumbJsonLd,
  pageMetadata,
  seoLandingPages,
  softwareApplicationJsonLd,
} from "@/lib/seo";

function findPage(slug: string) {
  return seoLandingPages.find((page) => page.slug === slug);
}

export const dynamicParams = false;

export function generateStaticParams() {
  return seoLandingPages.map((page) => ({ seoSlug: page.slug }));
}

type SeoRouteProps = { params: Promise<{ seoSlug: string }> };

export async function generateMetadata({ params }: SeoRouteProps): Promise<Metadata> {
  const { seoSlug } = await params;
  const page = findPage(seoSlug);
  if (!page) return {};
  return pageMetadata({
    title: page.title,
    description: page.description,
    path: `/${page.slug}`,
  });
}

export default async function SeoLandingPage({ params }: SeoRouteProps) {
  const { seoSlug } = await params;
  const page = findPage(seoSlug);
  if (!page) notFound();

  const productJsonLd = {
    ...softwareApplicationJsonLd,
    name: page.title.replace(" | Lightbulb", ""),
    url: absoluteUrl(`/${page.slug}`),
    description: page.description,
  };

  return (
    <main className="ld-page min-h-screen">
      <JsonLd data={productJsonLd} />
      <JsonLd
        data={breadcrumbJsonLd([
          { name: "Home", path: "/" },
          { name: page.h1, path: `/${page.slug}` },
        ])}
      />
      <Navbar />
      <section className="mx-auto max-w-5xl px-6 py-20 md:py-28">
        <p className="ld-mono text-xs uppercase tracking-[0.18em] text-[var(--midnight-muted)]">
          {page.kicker}
        </p>
        <h1 className="mt-5 font-editorial-serif text-5xl leading-[0.98] text-[var(--midnight)] md:text-7xl">
          {page.h1}
        </h1>
        <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--midnight-soft)]">
          {page.primary}
        </p>
        <div className="mt-10 grid gap-4 md:grid-cols-3">
          {page.bullets.map((bullet) => (
            <div key={bullet} className="editorial-card p-5 text-sm leading-6 text-[var(--midnight-soft)]">
              <strong className="block font-editorial-serif text-xl font-normal text-[var(--midnight)]">
                {bullet}
              </strong>
            </div>
          ))}
        </div>
        <div className="mt-10 flex flex-wrap gap-4">
          <Link href="/#start" className="editorial-btn editorial-btn--primary h-11 px-6">
            Start with Lightbulb
          </Link>
          <Link href="/marketplace/agents" className="editorial-btn h-11 px-6">
            Browse agents
          </Link>
        </div>
      </section>
      <Footer />
    </main>
  );
}
