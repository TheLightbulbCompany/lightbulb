import type { Metadata } from "next";

import { HOME_FAQ } from "./homeFaq";

import { SITE_URL, SUPPORT_EMAIL } from "./domain";

export { SITE_URL };
export const SITE_NAME = "Lightbulb";
export const OG_IMAGE = "/og-lightbulb.png";
export const HOME_TITLE = "The Number One Voice AI Application Company | Lightbulb";
export const HOME_DESCRIPTION =
  "Lightbulb Phone answers your calls and books appointments. Lightbulb Meetings takes notes in Google Meet. Lightbulb OS is a voice app for the Mac.";
export const PHONE_TITLE = "AI Receptionist for Salons, Spas & Barbershops | Lightbulb";
export const PHONE_DESCRIPTION =
  "Your salon's AI receptionist answers your calls day and night, books into Google Calendar, takes messages and hands callers to you. Keep your number.";

/**
 * The product in one paragraph and its key facts, for answer engines
 * (/llms.txt, /llms-full.txt). Every line is VERIFIED in
 * .agents/product-marketing.md; no price, minute count or trial is typed here.
 */
export const PRODUCT_SUMMARY =
  "Lightbulb gives a salon, spa or barbershop an AI receptionist with the name the owner chooses. " +
  "It answers the business's phone day and night, books, moves and cancels appointments, takes messages, " +
  "and puts anything it can't decide in front of the owner.";

export const PRODUCT_FACTS = [
  "Who it's for: independent salons (hair, nails, lashes, brows), day spas, massage studios and barbershops in the US.",
  "Answers calls 24/7, several at once (up to five live calls on one line), in English, or English and Spanish.",
  "Books, moves and cancels appointments during the call, inside the business's opening hours, for a staff member who does that service, with the service length and buffer the owner set. Earlier bookings are found, moved or cancelled only when the caller's carrier verifies their number; otherwise it takes a message.",
  "Bookings go into its own calendar, shown on the owner's Home, or into Google Calendar. It does not book into Square, Vagaro, Fresha, Boulevard, GlossGenius, Booksy, Mindbody or Acuity.",
  "Answers questions from what the owner entered: hours, closures, services, prices, staff and the owner's own answers. It reads the business's website at setup to draft these, and the owner checks the draft.",
  "When it doesn't know an answer or isn't allowed to act, it takes a message and a Needs you card goes to the owner. The owner answers once and it knows next time.",
  "Transfers a caller to the owner's mobile when they ask for a person, optionally only during opening hours.",
  "The owner sees every call: who called, when, a one-line summary, the steps it took and the transcript.",
  "The owner picks its name, writes its greeting, chooses one of three voices and decides what it may do (book, move or cancel, greet returning callers by name, answer questions).",
  "Keep your number by forwarding missed and busy calls, or all calls, from Verizon, AT&T, T-Mobile, a landline or a VoIP app; the app checks the forwarding works. Or get a new US local number.",
  "It doesn't take payments and removes card numbers from every record it keeps. Callers reach only their own bookings.",
  "Can join the owner's Google Meet when invited, speaks only when named, and keeps notes, decisions and follow-ups.",
  "Billing: one monthly plan per business with minutes included, then a per-minute rate. Month to month, a live minutes meter, and the owner chooses what happens when minutes run out. Cancel the renewal yourself in Settings. There is no free trial: going live needs a paid plan. See " + SITE_URL + "/pricing.",
  "Not a fit for: medical, dental or med-spa practices that handle health information, multi-location chains, businesses outside the US, outbound calling or text messaging.",
];

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
  description: PRODUCT_SUMMARY,
  disambiguatingDescription:
    "Lightbulb (thelightbulb.company) makes an AI receptionist for salons, spas and barbershops. Not TheLightbulb.ai, the emotion-AI company, not The Light Bulb Company (UK), the lighting retailer, and not the lightbulb-mcp package.",
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
  "@id": `${SITE_URL}/phone#software`,
  name: "Lightbulb Phone",
  applicationCategory: "BusinessApplication",
  applicationSubCategory: "AI receptionist",
  operatingSystem: "Web",
  url: `${SITE_URL}/phone`,
  brand: { "@id": `${SITE_URL}/#organization` },
  provider: { "@id": `${SITE_URL}/#organization` },
  areaServed: { "@type": "Country", name: "United States" },
  audience: { "@type": "BusinessAudience", audienceType: "Salons, spas and barbershops" },
  description: PRODUCT_SUMMARY,
  // VERIFIED claims only (.agents/product-marketing.md).
  featureList: [
    "Answers calls 24/7, several at once",
    "Books, moves and cancels appointments during the call, inside your opening hours",
    "Books into its own calendar or Google Calendar",
    "Takes messages and sends anything it can't decide to you as a Needs you card",
    "Transfers callers to your mobile when they want a person",
    "Shows every call with a summary, the steps it took and the transcript",
    "Your choice of name, greeting and one of three voices",
    "English, or English and Spanish",
    "Keep your number with call forwarding, or get a new US local number",
  ],
  // No `offers` on the /phone graph (seo.test.ts): the price is read from
  // the billing API on /pricing, never typed into markup.
};

function withoutContext(node: Record<string, unknown>) {
  const rest = { ...node };
  delete rest["@context"];
  return rest;
}

/** The company home's `@graph`: who we are, no product, FAQ or offers. */
export const hubJsonLd = {
  "@context": "https://schema.org",
  "@graph": [
    withoutContext(organizationJsonLd),
    withoutContext(websiteJsonLd),
    {
      "@type": "WebPage",
      "@id": `${SITE_URL}/#webpage`,
      url: SITE_URL,
      name: HOME_TITLE,
      isPartOf: { "@id": `${SITE_URL}/#website` },
      about: { "@id": `${SITE_URL}/#organization` },
    },
  ],
};

/** /phone's one `@graph` (05 Part B): its FAQ is the visible HomeFaq, word for word. */
export const phoneJsonLd = {
  "@context": "https://schema.org",
  "@graph": [
    withoutContext(organizationJsonLd),
    withoutContext(websiteJsonLd),
    withoutContext(softwareApplicationJsonLd),
    {
      "@type": "WebPage",
      "@id": `${SITE_URL}/phone#webpage`,
      url: `${SITE_URL}/phone`,
      name: PHONE_TITLE,
      isPartOf: { "@id": `${SITE_URL}/#website` },
      about: { "@id": `${SITE_URL}/phone#software` },
    },
    {
      "@type": "FAQPage",
      "@id": `${SITE_URL}/phone#faq`,
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
      name: "Lightbulb network documentation",
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

/** `FAQPage` for a FAQ the page renders word for word. */
export function faqJsonLd(faq: { q: string; a: string }[]) {
  return {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: faq.map(({ q, a }) => ({
      "@type": "Question",
      name: q,
      acceptedAnswer: { "@type": "Answer", text: a },
    })),
  };
}
