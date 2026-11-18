import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));

import { BANNED } from "@/content/__tests__/bannedCopy";

import { VerticalPage, verticalMetadata } from "../VerticalPage";
import { VERTICALS } from "../verticals";

describe("trade pages", () => {
  it("each has its own title, description, H1, calls, services and FAQ", () => {
    for (const key of ["title", "description", "h1", "intro", "busy", "servicesIntro"] as const) {
      expect(new Set(VERTICALS.map((v) => v[key])).size, key).toBe(3);
    }
    const asks = VERTICALS.flatMap((v) => v.calls.map((c) => c.ask));
    const questions = VERTICALS.flatMap((v) => v.faq.map((f) => f.q));
    expect(new Set(asks).size).toBe(asks.length);
    expect(new Set(questions).size).toBe(questions.length);
    for (const v of VERTICALS) {
      expect(v.title.length, v.slug).toBeLessThanOrEqual(60);
      expect(v.description.length, v.slug).toBeLessThanOrEqual(160);
      expect(verticalMetadata(v).alternates?.canonical).toBe(`https://thelightbulb.company${v.path}`);
      expect(JSON.stringify(v).match(BANNED)?.[0], v.slug).toBeUndefined();
    }
  });

  it.each(VERTICALS.map((v) => [v.slug, v] as const))("%s: one H1, FAQ schema matching the page, links to the other trades", (slug, v) => {
    const { container, unmount } = render(<VerticalPage v={v} />);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    const graphs = [...container.querySelectorAll('script[type="application/ld+json"]')].map((s) => JSON.parse(s.textContent!));
    const faq = graphs.find((g) => g["@type"] === "FAQPage");
    expect(faq.mainEntity.map((q: { name: string; acceptedAnswer: { text: string } }) => [q.name, q.acceptedAnswer.text])).toEqual(
      v.faq.map((f) => [f.q, f.a]),
    );
    expect(JSON.stringify(graphs)).not.toMatch(/aggregateRating|"review"|"offers"/i);
    for (const o of VERTICALS.filter((o) => o.slug !== slug)) {
      expect(container.querySelector(`a[href="${o.path}"]`)).not.toBeNull();
    }
    expect(screen.getAllByRole("link", { name: "Set up your assistant" })[0]).toHaveAttribute("href", `/sign-up?source=vertical-${slug}`);
    unmount();
  });
});
