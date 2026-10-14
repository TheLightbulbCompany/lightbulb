import type { MetadataRoute } from "next";

import { getAllPosts } from "@/content/blog";
import { getAllDocs } from "@/content/docs";
import { SITE_URL } from "@/lib/domain";
import { seoLandingPages } from "@/lib/seo";

/**
 * Public sitemap. Covers the marketing surface + blog. Private app routes
 * (/chat, /settings, /onboarding, /admin, /api) are intentionally excluded
 * and additionally disallowed in robots.ts.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  const staticRoutes: MetadataRoute.Sitemap = [
    { url: `${SITE_URL}/`, changeFrequency: "weekly", priority: 1 },
    { url: `${SITE_URL}/request-setup`, changeFrequency: "monthly", priority: 0.9 },
    { url: `${SITE_URL}/blog`, changeFrequency: "weekly", priority: 0.8 },
    { url: `${SITE_URL}/docs`, changeFrequency: "weekly", priority: 0.85 },
    { url: `${SITE_URL}/marketplace`, changeFrequency: "weekly", priority: 0.7 },
    { url: `${SITE_URL}/marketplace/agents`, changeFrequency: "weekly", priority: 0.6 },
    { url: `${SITE_URL}/support`, changeFrequency: "monthly", priority: 0.4 },
    { url: `${SITE_URL}/privacy`, changeFrequency: "yearly", priority: 0.2 },
    { url: `${SITE_URL}/terms`, changeFrequency: "yearly", priority: 0.2 },
    { url: `${SITE_URL}/cookies`, changeFrequency: "yearly", priority: 0.1 },
    { url: `${SITE_URL}/disclaimer`, changeFrequency: "yearly", priority: 0.1 },
    { url: `${SITE_URL}/eula`, changeFrequency: "yearly", priority: 0.1 },
    { url: `${SITE_URL}/acceptable-use`, changeFrequency: "yearly", priority: 0.1 },
    { url: `${SITE_URL}/accessibility`, changeFrequency: "yearly", priority: 0.1 },
  ];

  const blogRoutes: MetadataRoute.Sitemap = getAllPosts().map((post) => ({
    url: `${SITE_URL}/blog/${post.slug}`,
    lastModified: new Date(`${post.date}T00:00:00Z`),
    changeFrequency: "monthly",
    priority: 0.6,
  }));

  const docsRoutes: MetadataRoute.Sitemap = getAllDocs().map((doc) => ({
    url: `${SITE_URL}/docs/${doc.slug}`,
    changeFrequency: "monthly",
    priority: 0.7,
  }));

  const seoRoutes: MetadataRoute.Sitemap = seoLandingPages.map((page) => ({
    url: `${SITE_URL}/${page.slug}`,
    changeFrequency: "monthly",
    priority: 0.75,
  }));

  return [...staticRoutes, ...docsRoutes, ...seoRoutes, ...blogRoutes];
}
