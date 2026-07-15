import type { MetadataRoute } from "next";

import { getAllPosts } from "@/content/blog";

const SITE_URL = "https://isol8.co";

/**
 * Public sitemap. Covers the marketing surface + blog. Private app routes
 * (/chat, /settings, /onboarding, /admin, /api) are intentionally excluded
 * and additionally disallowed in robots.ts.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  const staticRoutes: MetadataRoute.Sitemap = [
    { url: `${SITE_URL}/`, changeFrequency: "weekly", priority: 1 },
    { url: `${SITE_URL}/blog`, changeFrequency: "weekly", priority: 0.8 },
    { url: `${SITE_URL}/marketplace`, changeFrequency: "weekly", priority: 0.7 },
    { url: `${SITE_URL}/support`, changeFrequency: "monthly", priority: 0.4 },
    { url: `${SITE_URL}/privacy`, changeFrequency: "yearly", priority: 0.2 },
    { url: `${SITE_URL}/terms`, changeFrequency: "yearly", priority: 0.2 },
  ];

  const blogRoutes: MetadataRoute.Sitemap = getAllPosts().map((post) => ({
    url: `${SITE_URL}/blog/${post.slug}`,
    lastModified: new Date(`${post.date}T00:00:00Z`),
    changeFrequency: "monthly",
    priority: 0.6,
  }));

  return [...staticRoutes, ...blogRoutes];
}
