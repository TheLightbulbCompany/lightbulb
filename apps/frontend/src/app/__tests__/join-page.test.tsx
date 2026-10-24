import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const capture = vi.hoisted(() => vi.fn());
vi.mock("@/lib/analytics", () => ({ capture }));

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));

import JoinPage, { metadata } from "../j/page";

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

  it.each(["#%27;rm -rf ~;%27", "#%0A", "#%", "#a%", "#caf\u00e9", "#a'b"])("refuses hostile fragment %s", async (hash) => {
    window.location.hash = hash;
    const { container } = render(<JoinPage />);
    expect(await screen.findByText(/not valid/i)).toBeTruthy();
    expect(container.textContent).not.toContain("lightbulb join");
  });

  it("fires view and copy events without the ticket", async () => {
    capture.mockClear();
    window.location.hash = "#ticket123";
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
    render(<JoinPage />);
    (await screen.findByRole("button", { name: /copy/i })).click();
    expect(capture.mock.calls).toEqual([["join_link_viewed", { valid: true }], ["join_command_copied"]]);
    expect(JSON.stringify(capture.mock.calls)).not.toContain("ticket123");
  });

  it("is noindex", () => {
    expect(metadata.robots).toEqual({ index: false, follow: false });
  });
});
