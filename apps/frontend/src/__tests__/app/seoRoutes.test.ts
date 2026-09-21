import { describe, expect, it } from "vitest";

import { GET as llms } from "@/app/llms.txt/route";
import { GET as llmsFull } from "@/app/llms-full.txt/route";
import { GET as rss } from "@/app/blog/rss.xml/route";
import { getAllPosts } from "@/content/blog";
import { getAllDocs } from "@/content/docs";
import { SITE_URL } from "@/lib/domain";
import { seoLandingPages } from "@/lib/seo";

/**
 * These three routes are GENERATED from the same content sources as
 * `sitemap.ts` so they cannot drift. The tests assert exactly that: add a doc
 * or a post and it must appear, without anyone editing a list by hand.
 */

async function body(res: Response) {
  return res.text();
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
    for (const page of seoLandingPages) {
      expect(text).toContain(`${SITE_URL}/${page.slug}`);
    }
  });

  it("is served as plain text and opens with the llms.txt H1 + summary", async () => {
    const res = llms();
    expect(res.headers.get("Content-Type")).toMatch(/^text\/plain/);
    const text = await body(res);
    expect(text.startsWith("# Lightbulb\n")).toBe(true);
    expect(text).toContain("\n> ");
  });

  it("states the plan facts the backend actually charges", async () => {
    const text = await body(llms());
    // Mirrors WORKSPACE_PRICE_USD / ULTRA_PRICE_USD and the trial_local cap.
    // A price edit in the backend must be reflected here, not silently drift.
    expect(text).toContain("$50/month");
    expect(text).toContain("$150/month");
    expect(text).toContain("Base plan: $20/month");
    expect(text).toContain("server-selected free model");
    expect(text).toContain("30 days");
    // The 14-day trial was removed in 2026-07; never reintroduce the claim.
    expect(text).not.toMatch(/14[- ]day/i);
  });
});

describe("/llms-full.txt", () => {
  it("carries the full markdown body of every doc and post", async () => {
    const text = await body(llmsFull());
    for (const doc of getAllDocs()) {
      expect(text).toContain(doc.content.trim().slice(0, 60));
    }
    for (const post of getAllPosts()) {
      expect(text).toContain(post.content.trim().slice(0, 60));
    }
  });
});

describe("/blog/rss.xml", () => {
  it("emits one well-formed item per post, newest first", async () => {
    const res = rss();
    expect(res.headers.get("Content-Type")).toMatch(/^application\/rss\+xml/);
    const text = await body(res);
    const posts = getAllPosts();
    expect(text.match(/<item>/g)).toHaveLength(posts.length);
    expect(text).toContain(`<atom:link href="${SITE_URL}/blog/rss.xml"`);
    for (const post of posts) {
      expect(text).toContain(`<guid isPermaLink="true">${SITE_URL}/blog/${post.slug}</guid>`);
    }
    // Newest-first ordering is what a reader shows at the top.
    const first = text.indexOf(`/blog/${posts[0].slug}<`);
    const second = text.indexOf(`/blog/${posts[1].slug}<`);
    expect(first).toBeGreaterThan(-1);
    expect(first).toBeLessThan(second);
  });

  it("escapes XML metacharacters in titles and descriptions", async () => {
    const text = await body(rss());
    // No raw & that isn't an entity — the classic feed-corrupting bug.
    expect(text).not.toMatch(/&(?!amp;|lt;|gt;|quot;|apos;)/);
  });
});
