import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));

import JoinPage from "../j/page";

afterEach(() => {
  window.location.hash = "";
});

describe("/j", () => {
  it("renders the join command from the hash", async () => {
    window.location.hash = "#ticket123";
    const { container } = render(<JoinPage />);
    expect(await screen.findByText(/lightbulb join 'ticket123'/)).toBeTruthy();
    expect(container.textContent).toContain("install.sh | sh && ~/.lightbulb/connect/bin/lightbulb join 'ticket123'");
    expect(container.textContent).toContain("join the Lightbulb network with this link");
    expect(screen.getByRole("link", { name: /network/i }).getAttribute("href")).toBe("/network");
  });

  it("shows a friendly empty state with no hash", async () => {
    render(<JoinPage />);
    expect(await screen.findByText(/no invite in it/i)).toBeTruthy();
  });
});
