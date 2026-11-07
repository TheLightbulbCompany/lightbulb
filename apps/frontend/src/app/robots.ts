import type { MetadataRoute } from "next";

import { SITE_URL } from "@/lib/domain";

/**
 * robots.txt — allow crawling of the public marketing + blog surface, keep
 * the authenticated app and API routes out of the index.
 */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      allow: "/",
      disallow: [
        "/chat",
        "/settings",
        "/onboarding",
        "/admin",
        "/api",
      ],
    },
    sitemap: `${SITE_URL}/sitemap.xml`,
    host: SITE_URL,
  };
}
