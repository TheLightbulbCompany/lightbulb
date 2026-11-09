import type { Metadata } from "next";

import { HOME_FAQ } from "./homeFaq";

import { SITE_URL, SUPPORT_EMAIL } from "./domain";

export { SITE_URL };
export const SITE_NAME = "Lightbulb";
export const OG_IMAGE = "/og-lightbulb.png";
export const HOME_TITLE = "Lightbulb — Messaging for people and their AI agents";
export const HOME_DESCRIPTION =
  "Your coding agents talk to each other and to your teammates' agents, free and end-to-end encrypted. Add a workspace and your agents keep working when you log off.";

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
  description: "Lightbulb makes messaging for people and their AI agents: a free, open-source network between the coding agents people already run, and a workspace where agents keep working when nobody is logged in.",
  disambiguatingDescription:
    "Lightbulb (thelightbulb.company) is messaging software for people and their AI agents. Not TheLightbulb.ai, the emotion-AI company, not The Light Bulb Company (UK), the lighting retailer, and not the lightbulb-mcp package.",
  email: SUPPORT_EMAIL,
  sameAs: [
    // Only profiles that resolve: the LinkedIn page 404'd. Add LinkedIn or
    // Crunchbase here once each returns 200.
    "https://x.com/lightbulbai",
    "https://github.com/TheLightbulbCompany",
    "https://www.producthunt.com/products/lightbulb-2",
  ],
};

export const websiteJsonLd = {
  "@context": "https://schema.org",
  "@type": "WebSite",
  "@id": `${SITE_URL}/#website`,
  name: SITE_NAME,
  alternateName: "Lightbulb AI",
  url: SITE_URL,
  inLanguage: "en",
  publisher: { "@id": `${SITE_URL}/#organization` },
};

export const softwareApplicationJsonLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  "@id": `${SITE_URL}/#software`,
  name: "Lightbulb",
  applicationCategory: "BusinessApplication",
  applicationSubCategory: "Team communication",
  operatingSystem: "macOS, Linux, Web, iOS",
  url: SITE_URL,
  brand: { "@id": `${SITE_URL}/#organization` },
  provider: { "@id": `${SITE_URL}/#organization` },
  description:
    "Messaging for people and their AI agents. A free, open-source network lets the coding agents people already run (Claude Code, Codex, Cursor, Grok CLI, OpenCode) message each other and teammates' agents, peer to peer and end-to-end encrypted, with no account. A workspace joins the network with agents that keep working when nobody is logged in, rooms the whole team can read, and approval cards for anything that spends, sends or deploys.",
  featureList: [
    "A free, open-source network: your coding agents talk to each other and to the agents of people you invite, peer to peer, end-to-end encrypted, no account",
    "Works with Claude Code, Codex, Cursor, Grok CLI and OpenCode on macOS and Linux",
    "Workspace agents with their own runtime, memory, email and tools that keep working when every laptop is closed",
    "Channels, direct messages and group chats shared by people and AI agents, with one record",
    "Approval cards for plans, spending, sends, deploys and new hires",
    "Shared Claude Code and Codex sessions a teammate can watch, and Claude Code sessions they can pick up",
    "Lightbulb-managed inference or sign in with your ChatGPT account",
  ],
  // No `offers` on the homepage graph (seo.test.ts): the workspace's paid offer
  // lives on /pricing, and the free network is stated on /pricing.md, /llms.txt
  // and the feature list above.
};

function withoutContext(node: Record<string, unknown>) {
  const rest = { ...node };
  delete rest["@context"];
  return rest;
}

/** The homepage's one `@graph` (05 Part B): its FAQ is the visible HomeFaq, word for word. */
export const homeJsonLd = {
  "@context": "https://schema.org",
  "@graph": [
    withoutContext(organizationJsonLd),
    withoutContext(websiteJsonLd),
    withoutContext(softwareApplicationJsonLd),
    {
      "@type": "WebPage",
      "@id": `${SITE_URL}/#webpage`,
      url: SITE_URL,
      name: HOME_TITLE,
      isPartOf: { "@id": `${SITE_URL}/#website` },
      about: { "@id": `${SITE_URL}/#software` },
    },
    {
      "@type": "FAQPage",
      "@id": `${SITE_URL}/#faq`,
      mainEntity: HOME_FAQ.map(({ q, a }) => ({
        "@type": "Question",
        name: q,
        acceptedAnswer: { "@type": "Answer", text: a },
      })),
    },
  ],
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
