import { describe, expect, it } from "vitest";

import { GET as llms } from "@/app/llms.txt/route";
import { GET as llmsFull } from "@/app/llms-full.txt/route";
import { getAllDocs } from "@/content/docs";
import { SITE_URL } from "@/lib/domain";
import sitemap from "@/app/sitemap";
import robots from "@/app/robots";

/**
 * These routes are GENERATED from the same content sources as `sitemap.ts`
 * so they cannot drift: add a doc and it must appear, without anyone editing
 * a list by hand.
 */

async function body(res: Response | Promise<Response>) {
  return (await res).text();
}

const RETIRED = ["/phone", "/meetings", "/pricing", "/compare", "/request-setup", "/blog", "/voice", "/chat"];

describe("/llms.txt", () => {
  it("lists every doc and Spark's page with an absolute URL", async () => {
    const text = await body(llms());
    for (const doc of getAllDocs()) {
      expect(text).toContain(`${SITE_URL}/docs/${doc.slug}`);
      expect(text).toContain(doc.description);
    }
    expect(text).toContain(`${SITE_URL}/os)`);
    for (const path of RETIRED) expect(text).not.toContain(`${SITE_URL}${path})`);
  });

  it("is served as plain text and opens with the llms.txt H1 + summary", async () => {
    const res = await llms();
    expect(res.headers.get("Content-Type")).toMatch(/^text\/plain/);
    const text = await body(res);
    expect(text.startsWith("# Lightbulb\n")).toBe(true);
    expect(text).toContain("\n> ");
    expect(text).not.toMatch(/receptionist|salon/i);
  });
});

describe("/llms-full.txt", () => {
  it("carries the full markdown of every doc", async () => {
    const text = await body(llmsFull());
    for (const doc of getAllDocs()) {
      expect(text).toContain(doc.content.trim().slice(0, 60));
    }
  });
});

describe("/sitemap.xml", () => {
  it("lists Spark and the network, and none of the retired pages", () => {
    const urls = sitemap().map((e) => e.url);
    for (const path of ["/", "/os", "/network", "/docs"]) expect(urls).toContain(`${SITE_URL}${path}`);
    for (const path of RETIRED) expect(urls).not.toContain(`${SITE_URL}${path}`);
    expect(new Set(urls).size).toBe(urls.length);
  });
});

describe("/robots.txt", () => {
  it("keeps the admin and the API out", () => {
    const rules = robots().rules;
    const disallow = (Array.isArray(rules) ? rules[0] : rules).disallow as string[];
    expect(disallow).toEqual(["/admin", "/api"]);
  });
});
