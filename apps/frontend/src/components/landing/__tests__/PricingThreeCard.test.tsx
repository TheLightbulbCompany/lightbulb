// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { VOICE_PLAN_NEUTRAL } from "@/lib/pricing";

import { PricingThreeCard } from "../PricingThreeCard";

const P = {
  voiceMonthlyUsd: 250,
  voiceIncludedMinutes: 1500,
  voiceOverageUsd: 0.35,
};

it("states the receptionist plan from the billing API, neutral without it", () => {
  const { rerender } = render(<PricingThreeCard pricing={P} />);
  expect(screen.getByText("$250 a month includes 1,500 minutes, then $0.35 a minute.")).toBeInTheDocument();
  rerender(<PricingThreeCard pricing={null} />);
  expect(screen.getByText(VOICE_PLAN_NEUTRAL)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /See what's included/ })).toHaveAttribute("href", "/pricing");
});
