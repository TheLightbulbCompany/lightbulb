// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/image", () => ({ default: () => null }));
vi.mock("@/lib/signupFlow", () => ({ captureSignupCta: vi.fn() }));
vi.mock("@/lib/analytics", () => ({ register: vi.fn() }));

import { AskAI } from "../AskAI";
import { Footer } from "../Footer";
import { FoundingTeams } from "../FoundingTeams";
import { Hero } from "../Hero";
import { HomeFaq } from "../HomeFaq";
import { HowItWorks } from "../HowItWorks";
import { ProblemBand } from "../ProblemBand";
import { ProofStories } from "../ProofStories";
import { WorkspaceBand } from "../WorkspaceBand";
import { ASK_AI_PROMPT } from "../askAiPrompt";

const BANNED = /multiplayer|AI-native|orchestration|\bbots?\b|\bprompts?\b|autonomous|Paperclip|Hermes|AgentCore|\bJev\b/i;

describe("homepage voice", () => {
  it("uses none of the banned words or internal names, and names no price", () => {
    const { container } = render(
      <>
        <Hero variant="A" />
        <ProblemBand />
        <HowItWorks />
        <WorkspaceBand />
        <ProofStories />
        <FoundingTeams />
        <HomeFaq />
        <AskAI />
        <Footer />
      </>,
    );
    const text = `${container.textContent} ${ASK_AI_PROMPT}`;
    expect(text).not.toMatch(BANNED);
    expect(text).not.toMatch(/\$\s?(20|50)\b/);
    expect(text).toContain("Anna’s Frozen Treats");
    expect(text).toContain("Second Catch");
    expect(text).toContain("in your team’s channels");
  });
});
