import { describe, expect, it } from "vitest";

import { extractPreviewUrls, linkDomain, linkPreviewPath } from "./linkPreview";

describe("extractPreviewUrls", () => {
  it("finds the first two distinct http(s) URLs and trims trailing punctuation", () => {
    expect(
      extractPreviewUrls("see https://a.com/x. and [b](https://b.com/y?q=1), again https://a.com/x and https://c.com"),
    ).toEqual(["https://a.com/x", "https://b.com/y?q=1"]);
  });

  it("ignores non-http schemes and text without links", () => {
    expect(extractPreviewUrls("ftp://a.com javascript:alert(1) plain words")).toEqual([]);
  });

  it("never unfurls our own domain or its subdomains", () => {
    expect(
      extractPreviewUrls("https://thelightbulb.company https://company.thelightbulb.company/ISOA/issues/ISOA-400 https://notthelightbulb.company https://dev.THELIGHTBULB.company/x https://flickmeon.ai/launch"),
    ).toEqual(["https://notthelightbulb.company"]);
  });

  it("stops at an autolink's closing bracket", () => {
    expect(extractPreviewUrls("<https://x.com/jack/status/20>")).toEqual(["https://x.com/jack/status/20"]);
  });
});

describe("fenced blocks", () => {
  it("a URL inside a fenced block gets no preview: the card draws its own link", () => {
    const text = "See below.\n```openui-lang\nroot = Stack([s])\ns = Sources([Source(\"Stripe\", \"https://docs.stripe.com/billing\")])\n```\nand https://example.com/after";
    expect(extractPreviewUrls(text)).toEqual(["https://example.com/after"]);
    const tilde = "~~~openui-lang\nroot = Stack([s])\ns = Sources([Source(\"Stripe\", \"https://docs.stripe.com/billing\")])\n~~~\nhttps://example.com/after";
    expect(extractPreviewUrls(tilde)).toEqual(["https://example.com/after"]);
  });
});

describe("helpers", () => {
  it("encodes the url into the endpoint path", () => {
    expect(linkPreviewPath("https://a.com/?a=1&b=2")).toBe("/messaging/link-preview?url=https%3A%2F%2Fa.com%2F%3Fa%3D1%26b%3D2");
  });

  it("strips www from the display domain", () => {
    expect(linkDomain("https://www.example.com/a")).toBe("example.com");
  });
});
