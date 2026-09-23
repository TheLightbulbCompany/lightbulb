import { describe, expect, it } from "vitest";
import { plainPreview } from "../plainPreview";

describe("plainPreview", () => {
  it("drops the markdown a notification body would show raw", () => {
    const reply = "Great question.\n\n---\n\n## Fresh TrackE — This Week's Focus\n\nYou're selling **handmade mugs**, which puts you in the *gift* market.";
    expect(plainPreview(reply)).toBe(
      "Great question. Fresh TrackE — This Week's Focus You're selling handmade mugs, which puts you in the gift market.",
    );
  });

  it("keeps link text, list items and code text", () => {
    expect(plainPreview("- see [the docs](https://x.y)\n1. run `pnpm test`\n```ts\nconst a = 1;\n```")).toBe(
      "see the docs run pnpm test const a = 1;",
    );
  });

  it("leaves plain text and a lone asterisk alone", () => {
    expect(plainPreview("5 * 3 = 15, snake_case stays")).toBe("5 * 3 = 15, snake_case stays");
  });
});
