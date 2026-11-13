// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/image", () => ({ default: () => null }));
const captureSignupCta = vi.fn();
vi.mock("@/lib/signupFlow", () => ({ captureSignupCta: (o: unknown) => captureSignupCta(o) }));
vi.mock("@/lib/analytics", () => ({ register: vi.fn(), capture: vi.fn() }));

import { AskAI } from "../AskAI";
import { Footer } from "../Footer";
import { FoundingTeams } from "../FoundingTeams";
import { Hero } from "../Hero";
import { HomeFaq } from "../HomeFaq";
import { HowItWorks } from "../HowItWorks";
import { NetworkBand } from "../NetworkBand";
import { ProblemBand } from "../ProblemBand";
import { ProofStories } from "../ProofStories";
import { WorkspaceBand } from "../WorkspaceBand";
import { ASK_AI_PROMPT } from "../askAiPrompt";

const PRICING = { teamPriceUsd: 50, includedMembers: 5, seatPriceUsd: 20, trialDays: 30, frontDeskPriceUsd: null, managedMarkup: "1.2", voiceMonthlyUsd: 250, voiceIncludedMinutes: 1500, voiceOverageUsd: 0.35 };
const BANNED = /multiplayer|AI-native|orchestration|\bbots?\b|\bprompts?\b|autonomous|Paperclip|Hermes|AgentCore|\bJev\b/i;

describe("homepage voice", () => {
  it("uses none of the banned words or internal names, and names a price only in the founding line", () => {
    const { container } = render(
      <>
        <Hero />
        <ProblemBand />
        <NetworkBand />
        <HowItWorks />
        <WorkspaceBand />
        <ProofStories />
        <FoundingTeams pricing={PRICING} />
        <HomeFaq />
        <AskAI />
        <Footer />
      </>,
    );
    const text = `${container.textContent} ${ASK_AI_PROMPT}`;
    expect(text).not.toMatch(BANNED);
    // The founding line's price comes from the billing API; retired plan prices never appear.
    expect(text).not.toMatch(/\$\s?150\b|\bUltra\b|\bBase\b/);
    expect(text).toContain("$250 a month includes 1,500 call minutes, then $0.35 a minute.");
    expect(text).toContain("Anna’s Frozen Treats");
    expect(text).toContain("Second Catch");
    expect(text).toContain("Mention one in a room.");
    expect(text).toContain("Start with the agents on your laptop.");
  });

  it("tracks both footer doors", () => {
    render(<Footer />);
    fireEvent.click(screen.getByRole("link", { name: /Request a guided setup/ }));
    fireEvent.click(screen.getByRole("link", { name: "Start free" }));
    expect(captureSignupCta.mock.calls.map(([o]) => o.ctaId)).toEqual(["footer_setup", "footer_start_free"]);
    captureSignupCta.mockClear();
  });

  it("tracks the founding-teams door", () => {
    render(<FoundingTeams pricing={PRICING} />);
    fireEvent.click(screen.getByRole("link", { name: /Request a guided setup/ }));
    expect(captureSignupCta.mock.calls.map(([o]) => o.ctaId)).toEqual(["founding_setup"]);
  });
});
