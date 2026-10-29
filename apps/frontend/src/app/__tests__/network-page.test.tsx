import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));

import NetworkPage from "../network/page";

describe("/network", () => {
  it("has the tell-your-agent line and both installs", () => {
    const { container } = render(<NetworkPage />);
    const text = container.textContent ?? "";
    expect(text).toContain("Tell your agent: Join the Lightbulb network: https://thelightbulb.company/network");
    expect(text).toContain("install.sh | sh");
    expect(text).toContain("brew install thelightbulbcompany/tap/lightbulb");
    expect(text).toContain("four-word code");
    expect(text).toContain("An invite is the approval");
    expect(text).not.toContain("approve J1");
    expect(screen.getByRole("link", { name: /start a workspace/i }).getAttribute("href")).toBe("/sign-up");
  });
});
