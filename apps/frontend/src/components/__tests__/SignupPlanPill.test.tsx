import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SignupPlanPill } from "../SignupPlanPill";

describe("SignupPlanPill", () => {
  it("offers a no-card workspace trial", () => {
    render(<SignupPlanPill />);
    expect(screen.getByText("Free for 30 days")).toBeInTheDocument();
    expect(screen.getByText(/no credit card/i)).toBeInTheDocument();
  });
});
