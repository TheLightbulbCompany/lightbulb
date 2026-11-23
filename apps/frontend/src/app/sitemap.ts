import type { MetadataRoute } from "next";

import { getAllPosts } from "@/content/blog";
import { COMPARE_PAGES, COMPARE_UPDATED } from "@/content/compare";
import { getAllDocs } from "@/content/docs";
import { SITE_URL } from "@/lib/domain";

/**
 * Public sitemap: the three products' pages first, then the developer network
 * docs, then legal. Private app routes (/chat, /settings, /onboarding, /admin,
 * /api) are excluded here and disallowed in robots.ts.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  const posts = getAllPosts();
  const compareDate = new Date(`${COMPARE_UPDATED}T00:00:00Z`);

  const product: MetadataRoute.Sitemap = [
    { url: `${SITE_URL}/`, changeFrequency: "weekly", priority: 1 },
    { url: `${SITE_URL}/phone`, changeFrequency: "weekly", priority: 0.95 },
    { url: `${SITE_URL}/meetings`, changeFrequency: "monthly", priority: 0.8 },
    { url: `${SITE_URL}/os`, changeFrequency: "monthly", priority: 0.6 },
    { url: `${SITE_URL}/pricing`, changeFrequency: "monthly", priority: 0.9 },
    { url: `${SITE_URL}/request-setup`, changeFrequency: "monthly", priority: 0.7 },
    { url: `${SITE_URL}/compare`, lastModified: compareDate, changeFrequency: "monthly", priority: 0.7 },
    ...COMPARE_PAGES.map((page) => ({
      url: `${SITE_URL}${page.path}`,
      lastModified: compareDate,
      changeFrequency: "monthly" as const,
      priority: 0.75,
    })),
    // An empty blog index is noindex; list it again with its first post.
    ...(posts.length ? [{ url: `${SITE_URL}/blog`, changeFrequency: "weekly" as const, priority: 0.6 }] : []),
    ...posts.map((post) => ({
      url: `${SITE_URL}/blog/${post.slug}`,
      lastModified: new Date(`${post.date}T00:00:00Z`),
      changeFrequency: "monthly" as const,
      priority: 0.6,
    })),
    { url: `${SITE_URL}/support`, changeFrequency: "monthly", priority: 0.4 },
  ];

  // The free developer network: kept and listed, not the product's front door.
  const network: MetadataRoute.Sitemap = [
    { url: `${SITE_URL}/network`, changeFrequency: "monthly", priority: 0.4 },
    { url: `${SITE_URL}/docs`, changeFrequency: "monthly", priority: 0.4 },
    ...getAllDocs().map((doc) => ({
      url: `${SITE_URL}/docs/${doc.slug}`,
      changeFrequency: "monthly" as const,
      priority: 0.3,
    })),
  ];

  const legal: MetadataRoute.Sitemap = [
    "privacy",
    "terms",
    "cookies",
    "disclaimer",
    "eula",
    "acceptable-use",
    "accessibility",
  ].map((p) => ({ url: `${SITE_URL}/${p}`, changeFrequency: "yearly" as const, priority: 0.1 }));

  return [...product, ...network, ...legal];
}
