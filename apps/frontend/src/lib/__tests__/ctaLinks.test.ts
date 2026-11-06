import { describe, expect, it } from "vitest";
import { isPlaceholderUrl, normalizeCtaLinks, plainCta, stripMetaLines } from "../ctaLinks";

// The walk-17 florist email, as GET /tasks/095ed80d…/outputs[0].ref.text returned it on dev (6 Nov 2026).
const WALK17_EMAIL = [
  "Subject: Spend Saturday arranging flowers with us", "",
  "Preview text: Join Dana Whitfield for a flower-arranging workshop, $65 per person.", "",
  "We’d love to welcome you back to the shop for one of our Saturday flower-arranging workshops.", "",
  "Dana Whitfield will guide you through the session. Tickets are $65 per person.", "",
  "[**BOOK YOUR SPOT**]([BOOKING LINK])",
].join("\n");

describe("normalizeCtaLinks (W17-3)", () => {
  it("turns the walk-17 email's button into a CTA line, and Copy into its words", () => {
    expect(normalizeCtaLinks(WALK17_EMAIL).split("\n").at(-1)).toBe("[BOOK YOUR SPOT]");
    expect(plainCta(WALK17_EMAIL).split("\n").at(-1)).toBe("BOOK YOUR SPOT");
    // Copy keeps the rest word for word, less the "Preview text:" line (W18).
    expect(plainCta(WALK17_EMAIL).split("\n").slice(0, -1).join("\n"))
      .toBe(WALK17_EMAIL.split("\n").slice(0, -1).filter((line) => !line.startsWith("Preview text:")).join("\n"));
  });

  it.each([
    ["[**BOOK YOUR SPOT**]([BOOKING LINK])", "[BOOK YOUR SPOT]"],
    ["[BOOK YOUR SPOT]([BOOKING LINK])", "[BOOK YOUR SPOT]"],
    ["[Reserve a spot](REGISTRATION_URL)", "[Reserve a spot]"],
    ["[Reserve a spot](BOOKING LINK)", "[Reserve a spot]"],
    ["[Book now]({{booking_url}})", "[Book now]"],
    ["[Book now](<BOOKING LINK>)", "[Book now]"],
    ["[Book now]()", "[Book now]"],
    ["[Book now](#)", "[Book now]"],
    ["**[Book now](LINK)**", "[Book now]"],
    ["[__Book now__](LINK)", "[Book now]"],
    ["[*Book now →*]([link])", "[Book now →]"],
    ["- [Book now](LINK)", "- [Book now]"],
    ["[**Sign up for story-time**]", "[Sign up for story-time]"],
    ["[Sign up for story-time]", "[Sign up for story-time]"],
    // Inside a sentence: the words, no button.
    ["Ready? [**Book here**]([BOOKING LINK]) before Friday.", "Ready? Book here before Friday."],
    ["Call us or [book online](BOOKING_URL).", "Call us or book online."],
  ])("%s → %s", (input, out) => expect(normalizeCtaLinks(input)).toBe(out));

  it.each([
    "[Book now](https://example.com/book)",
    "[Site](example.com)",
    "[Growth](mention:agt-1)",
    "[Notes](/chat?panel=files)",
    "[Top](#pricing)",
    "![Logo](LOGO)",
    "- [ ] Pick flowers",
    "- [x] Pick flowers",
    "```\n[Book](LINK)\n```",
    "Use `[Book](LINK)` in the template.",
    "[Docs]\n\n[Docs]: https://example.com/docs",
  ])("leaves %s untouched", (input) => expect(normalizeCtaLinks(input)).toBe(input));

  it("knows a placeholder from a link", () => {
    for (const url of ["", " ", "#", "[BOOKING LINK]", "{{link}}", "{link}", "<BOOKING LINK>", "BOOKING_URL", "link-here"]) expect(isPlaceholderUrl(url)).toBe(true);
    for (const url of ["https://a.b", "mailto:a@b.c", "mention:x", "/a", "#top", "?q=1", "./a.md", "shop.example.com"]) expect(isPlaceholderUrl(url)).toBe(false);
  });
});

describe("stripMetaLines", () => {
  it("drops an email's preview-text and preheader lines, keeps the subject and body (W18)", () => {
    const email = "Subject: Tea tasting with Mei\n\n**Preview text:** Join us for tea\nPreheader: tins\n\nHi all,\nCome taste.";
    expect(stripMetaLines(email)).toBe("Subject: Tea tasting with Mei\n\n\nHi all,\nCome taste.");
    expect(plainCta("Hi\nPreview text: x")).toBe("Hi\n");
    expect(stripMetaLines("We preview text: drafts first.")).toBe("We preview text: drafts first.");
  });
});
