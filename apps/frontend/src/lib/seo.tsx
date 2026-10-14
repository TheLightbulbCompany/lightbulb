import type { Metadata } from "next";

import { SITE_URL } from "./domain";

export { SITE_URL };
export const SITE_NAME = "Lightbulb";
export const OG_IMAGE = "/og-lightbulb.png";
export const HOME_TITLE = "Lightbulb AI — Shared Workspace for Humans and AI Agents";
export const HOME_DESCRIPTION =
  "Lightbulb is a shared workspace where people and AI agents work together in channels, with persistent context, tasks, approvals, and connected tools.";

export function absoluteUrl(path = "/") {
  return new URL(path, SITE_URL).toString();
}

export function pageMetadata({
  title,
  description,
  path,
}: {
  title: string;
  description: string;
  path: string;
}): Metadata {
  const url = absoluteUrl(path);
  return {
    title,
    description,
    alternates: { canonical: url },
    openGraph: {
      title,
      description,
      url,
      siteName: SITE_NAME,
      type: "website",
      images: [{ url: OG_IMAGE, width: 1200, height: 630, alt: SITE_NAME }],
    },
    twitter: {
      card: "summary_large_image",
      title,
      description,
      images: [OG_IMAGE],
    },
  };
}

export function JsonLd({ data }: { data: unknown }) {
  return (
    <script
      type="application/ld+json"
      dangerouslySetInnerHTML={{ __html: JSON.stringify(data).replace(/</g, "\\u003c") }}
    />
  );
}

export const organizationJsonLd = {
  "@context": "https://schema.org",
  "@type": "Organization",
  "@id": `${SITE_URL}/#organization`,
  name: "Lightbulb AI",
  legalName: "Isol8 Inc.",
  alternateName: ["Lightbulb", "The Lightbulb Company"],
  url: SITE_URL,
  logo: absoluteUrl("/icon.png"),
  sameAs: [
    // Only profiles that resolve: the LinkedIn page 404'd. Add LinkedIn,
    // Crunchbase or Product Hunt here once each returns 200.
    "https://x.com/lightbulbai",
  ],
};

export const websiteJsonLd = {
  "@context": "https://schema.org",
  "@type": "WebSite",
  "@id": `${SITE_URL}/#website`,
  name: SITE_NAME,
  alternateName: "Lightbulb AI",
  url: SITE_URL,
  publisher: { "@id": `${SITE_URL}/#organization` },
  description:
    "Lightbulb gives companies persistent AI agents, an agent workspace, and an agent marketplace for autonomous operations.",
};

export const softwareApplicationJsonLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  "@id": `${SITE_URL}/#software`,
  name: "Lightbulb",
  applicationCategory: "BusinessApplication",
  operatingSystem: "Web",
  url: SITE_URL,
  brand: { "@id": `${SITE_URL}/#organization` },
  provider: { "@id": `${SITE_URL}/#organization` },
  description:
    "An AI agent workspace for running company operations with persistent, cloud-isolated agents.",
  offers: {
    "@type": "Offer",
    price: "0",
    priceCurrency: "USD",
  },
};

/**
 * `TechArticle` for a single documentation page.
 *
 * Docs are product documentation, not news, so `TechArticle` is the correct
 * type (Google treats it as an Article subtype; answer engines use `about` /
 * `articleSection` to place the page). `isPartOf` points back at the `/docs`
 * `CollectionPage` so the set reads as one corpus rather than five orphans.
 *
 * `dateModified` is deliberately omitted: docs carry no date in frontmatter,
 * and emitting a build timestamp would claim every page changed on every
 * deploy — a freshness signal that is simply untrue.
 */
export function techArticleJsonLd({
  title,
  description,
  path,
  section,
}: {
  title: string;
  description: string;
  path: string;
  section?: string;
}) {
  return {
    "@context": "https://schema.org",
    "@type": "TechArticle",
    headline: title,
    description,
    url: absoluteUrl(path),
    mainEntityOfPage: { "@type": "WebPage", "@id": absoluteUrl(path) },
    inLanguage: "en",
    ...(section ? { articleSection: section } : {}),
    isPartOf: {
      "@type": "CollectionPage",
      name: "Lightbulb product documentation",
      url: absoluteUrl("/docs"),
    },
    author: { "@type": "Organization", name: SITE_NAME, url: SITE_URL },
    publisher: {
      "@type": "Organization",
      name: SITE_NAME,
      url: SITE_URL,
      logo: { "@type": "ImageObject", url: absoluteUrl("/icon.png") },
    },
  };
}

export function breadcrumbJsonLd(items: { name: string; path: string }[]) {
  return {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: items.map((item, index) => ({
      "@type": "ListItem",
      position: index + 1,
      name: item.name,
      item: absoluteUrl(item.path),
    })),
  };
}

// The other nine thin pages were retired with 308s in `next.config.ts`
// (RETIRED_SEO_REDIRECTS). This one stays until its comparison page ships.
export const seoLandingPages = [
  ["ai-agents-for-slack", "AI Agents for Slack Workflows | Lightbulb", "Bring AI agents into Slack-driven operations", "Use Lightbulb to connect agent work with Slack-heavy teams, routing tasks and updates into governed operational workflows.", "Slack workflows", "For teams that live in Slack, Lightbulb provides the agent workspace behind the scenes: task intake, execution, approvals, and status.", ["Slack-aware task routing", "Agent status updates", "Approval-friendly collaboration"]],
].map(([slug, title, h1, description, kicker, primary, bullets]) => ({ slug, title, h1, description, kicker, primary, bullets })) as {
  slug: string;
  title: string;
  h1: string;
  description: string;
  kicker: string;
  primary: string;
  bullets: string[];
}[];

export type SeoLandingPage = (typeof seoLandingPages)[number];
