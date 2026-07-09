import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";
import { ClawHubSearch } from "../ClawHubSearch";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

const RESULT = {
  slug: "@acme/pdf-tools",
  displayName: "PDF Tools",
  summary: "Split, merge, and extract text from PDFs.",
  version: "1.2.0",
};

describe("ClawHubSearch", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders nothing until there is a query", () => {
    const { container } = render(
      <ClawHubSearch query="   " callRpc={vi.fn()} onInstalled={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("searches ClawHub and renders results", async () => {
    const callRpc = vi.fn().mockResolvedValue({ results: [RESULT] });
    render(<ClawHubSearch query="pdf" callRpc={callRpc} onInstalled={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /Search ClawHub for/i }));

    expect(callRpc).toHaveBeenCalledWith("skills.search", { query: "pdf", limit: 20 });
    expect(await screen.findByText("PDF Tools")).toBeInTheDocument();
    expect(screen.getByText("@acme/pdf-tools")).toBeInTheDocument();
  });

  it("installs a skill from ClawHub by ref", async () => {
    const callRpc = vi
      .fn()
      .mockResolvedValueOnce({ results: [RESULT] }) // search
      .mockResolvedValueOnce({ ok: true }); // install
    const onInstalled = vi.fn();
    render(<ClawHubSearch query="pdf" callRpc={callRpc} onInstalled={onInstalled} />);

    fireEvent.click(screen.getByRole("button", { name: /Search ClawHub for/i }));
    fireEvent.click(await screen.findByRole("button", { name: /^Install$/i }));

    await waitFor(() =>
      expect(callRpc).toHaveBeenCalledWith(
        "skills.install",
        { source: "clawhub", slug: "@acme/pdf-tools" },
        120_000,
      ),
    );
    expect(onInstalled).toHaveBeenCalled();
    expect(await screen.findByText(/appear in your skills shortly/i)).toBeInTheDocument();
  });

  it("drops stale results when the query changes", async () => {
    const callRpc = vi.fn().mockResolvedValue({ results: [RESULT] });
    const { rerender } = render(
      <ClawHubSearch query="pdf" callRpc={callRpc} onInstalled={vi.fn()} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Search ClawHub for/i }));
    expect(await screen.findByText("PDF Tools")).toBeInTheDocument();

    rerender(<ClawHubSearch query="calendar" callRpc={callRpc} onInstalled={vi.fn()} />);

    // Old results gone; the button reflects the new query, not the stale one.
    expect(screen.queryByText("PDF Tools")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Search ClawHub for “calendar”/i })).toBeInTheDocument();
  });

  it("offers an explicit override when the install is blocked on trust, and acknowledges the risk", async () => {
    const callRpc = vi
      .fn()
      .mockResolvedValueOnce({ results: [RESULT] }) // search
      .mockRejectedValueOnce(new Error("ClawHub trust check failed — acknowledge the risk to proceed"))
      .mockResolvedValueOnce({ ok: true }); // install anyway
    render(<ClawHubSearch query="pdf" callRpc={callRpc} onInstalled={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /Search ClawHub for/i }));
    fireEvent.click(await screen.findByRole("button", { name: /^Install$/i }));

    const overrideBtn = await screen.findByRole("button", { name: /Install anyway/i });
    fireEvent.click(overrideBtn);

    await waitFor(() =>
      expect(callRpc).toHaveBeenLastCalledWith(
        "skills.install",
        { source: "clawhub", slug: "@acme/pdf-tools", acknowledgeClawHubRisk: true },
        120_000,
      ),
    );
  });
});
