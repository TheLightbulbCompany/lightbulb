import type { MetadataRoute } from "next";

import { getAllDocs } from "@/content/docs";
import { SITE_URL } from "@/lib/domain";

/**
 * Public sitemap: Spark first, then the developer network docs, then legal.
 * /admin and /api are excluded here and disallowed in robots.ts.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  const product: MetadataRoute.Sitemap = [
    { url: `${SITE_URL}/`, changeFrequency: "weekly", priority: 1 },
    { url: `${SITE_URL}/os`, changeFrequency: "weekly", priority: 0.9 },
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
