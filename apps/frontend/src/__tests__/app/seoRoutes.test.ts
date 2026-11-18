import { describe, expect, it } from "vitest";

import { GET as llms } from "@/app/llms.txt/route";
import { GET as llmsFull } from "@/app/llms-full.txt/route";
import { GET as rss } from "@/app/blog/rss.xml/route";
import { getAllPosts } from "@/content/blog";
import { getAllDocs } from "@/content/docs";
import { SITE_URL } from "@/lib/domain";
import { COMPARE_PAGES } from "@/content/compare";
import { VERTICALS } from "@/components/vertical/verticals";
import sitemap from "@/app/sitemap";
import { BANNED } from "@/content/__tests__/bannedCopy";

/**
 * These three routes are GENERATED from the same content sources as
 * `sitemap.ts` so they cannot drift. The tests assert exactly that: add a doc
 * or a post and it must appear, without anyone editing a list by hand.
 */

async function body(res: Response | Promise<Response>) {
  return (await res).text();
}

describe("/llms.txt", () => {
  it("lists every doc, post, and landing page with an absolute URL", async () => {
    const text = await body(llms());
    for (const doc of getAllDocs()) {
      expect(text).toContain(`${SITE_URL}/docs/${doc.slug}`);
      expect(text).toContain(doc.description);
    }
    for (const post of getAllPosts()) {
      expect(text).toContain(`${SITE_URL}/blog/${post.slug}`);
    }
    for (const page of COMPARE_PAGES) {
      expect(text).toContain(`${SITE_URL}${page.path}`);
    }
  });

  it("is served as plain text and opens with the llms.txt H1 + summary", async () => {
    const res = await llms();
    expect(res.headers.get("Content-Type")).toMatch(/^text\/plain/);
    const text = await body(res);
    expect(text.startsWith("# Lightbulb\n")).toBe(true);
    expect(text).toContain("\n> ");
  });

  it("states the receptionist's facts with no price, trial or old-product claim", async () => {
    const text = await body(llms());
    const product = text.slice(0, text.indexOf("## Developer network"));
    expect(product).toContain("AI receptionist");
    for (const v of VERTICALS) expect(product).toContain(`${SITE_URL}${v.path}`);
    expect(product).toContain("There is no free trial");
    // Only the developer section may say "agents"; no price is ever typed.
    expect(product.replace("There is no free trial", "").match(BANNED)?.[0]).toBeUndefined();
    expect(text).not.toMatch(/six|AI employees|CEO|seat/i);
  });
});

describe("/llms-full.txt", () => {
  it("carries every trade page, comparison, and the full markdown of every doc and post", async () => {
    const text = await body(llmsFull());
    for (const v of VERTICALS) for (const f of v.faq) expect(text).toContain(f.a);
    for (const p of COMPARE_PAGES) expect(text).toContain(p.intro);
    for (const doc of getAllDocs()) {
      expect(text).toContain(doc.content.trim().slice(0, 60));
    }
    for (const post of getAllPosts()) {
      expect(text).toContain(post.content.trim().slice(0, 60));
    }
  });
});

describe("/sitemap.xml", () => {
  it("lists the trade pages and comparisons, and none of the retired pages", () => {
    const urls = sitemap().map((e) => e.url);
    for (const v of VERTICALS) expect(urls).toContain(`${SITE_URL}${v.path}`);
    for (const p of COMPARE_PAGES) expect(urls).toContain(`${SITE_URL}${p.path}`);
    expect(urls).toContain(`${SITE_URL}/network`);
    expect(urls).toContain(`${SITE_URL}/docs`);
    expect(urls).toContain(`${SITE_URL}/request-setup`);
    for (const gone of ["/compare/slack", "/docs/get-started"]) {
      expect(urls).not.toContain(`${SITE_URL}${gone}`);
    }
    expect(new Set(urls).size).toBe(urls.length);
  });
});

describe("/blog/rss.xml", () => {
  it("emits one well-formed item per post, newest first", async () => {
    const res = rss();
    expect(res.headers.get("Content-Type")).toMatch(/^application\/rss\+xml/);
    const text = await body(res);
    const posts = getAllPosts();
    expect(text.match(/<item>/g) ?? []).toHaveLength(posts.length);
    expect(text).toContain(`<atom:link href="${SITE_URL}/blog/rss.xml"`);
    for (const post of posts) {
      expect(text).toContain(`<guid isPermaLink="true">${SITE_URL}/blog/${post.slug}</guid>`);
    }
    // Newest-first ordering is what a reader shows at the top.
    if (posts.length > 1) {
      expect(text.indexOf(`/blog/${posts[0].slug}<`)).toBeLessThan(text.indexOf(`/blog/${posts[1].slug}<`));
    }
  });

  it("escapes XML metacharacters in titles and descriptions", async () => {
    const text = await body(rss());
    // No raw & that isn't an entity — the classic feed-corrupting bug.
    expect(text).not.toMatch(/&(?!amp;|lt;|gt;|quot;|apos;)/);
  });
});
