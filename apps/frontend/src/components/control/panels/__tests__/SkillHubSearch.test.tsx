import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import React from "react";
import { SkillHubSearch } from "../SkillHubSearch";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

const RESULT = {
  slug: "@acme/pdf-tools",
  displayName: "PDF Tools",
  summary: "Split, merge, and extract text from PDFs.",
  version: "1.2.0",
};

describe("SkillHubSearch", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders nothing until there is a query", () => {
    const { container } = render(
      <SkillHubSearch query="   " callRpc={vi.fn()} onInstalled={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("searches ClawHub and renders results", async () => {
    const callRpc = vi.fn().mockResolvedValue({ results: [RESULT] });
    render(<SkillHubSearch query="pdf" callRpc={callRpc} onInstalled={vi.fn()} />);

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
    render(<SkillHubSearch query="pdf" callRpc={callRpc} onInstalled={onInstalled} />);

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
      <SkillHubSearch query="pdf" callRpc={callRpc} onInstalled={vi.fn()} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Search ClawHub for/i }));
    expect(await screen.findByText("PDF Tools")).toBeInTheDocument();

    rerender(<SkillHubSearch query="calendar" callRpc={callRpc} onInstalled={vi.fn()} />);

    // Old results gone; the button reflects the new query, not the stale one.
    expect(screen.queryByText("PDF Tools")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Search ClawHub for “calendar”/i })).toBeInTheDocument();
  });

  it("ignores an in-flight search that resolves after the query changed", async () => {
    let resolveSearch: (v: unknown) => void = () => {};
    const callRpc = vi.fn().mockImplementation(
      () => new Promise((res) => { resolveSearch = res as (v: unknown) => void; }),
    );
    const { rerender } = render(
      <SkillHubSearch query="pdf" callRpc={callRpc} onInstalled={vi.fn()} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Search ClawHub for/i }));
    // Query changes while the "pdf" search is still in flight.
    rerender(<SkillHubSearch query="calendar" callRpc={callRpc} onInstalled={vi.fn()} />);
    // The stale search now resolves — its results must be dropped.
    await act(async () => {
      resolveSearch({ results: [RESULT] });
    });

    expect(screen.queryByText("PDF Tools")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Search ClawHub for “calendar”/i })).toBeInTheDocument();
  });

  it("searches and installs from HERMES's hub when that is the runtime", async () => {
    const hermesRow = {
      name: "pdf",
      description: "Read PDFs",
      identifier: "official/pdf",
      source: "official",
      trust: "builtin",
    };
    const callRpc = vi
      .fn()
      .mockResolvedValueOnce({ results: [hermesRow] })
      .mockResolvedValueOnce({ ok: true });
    render(
      <SkillHubSearch query="pdf" callRpc={callRpc} onInstalled={vi.fn()} hub="hermes" />,
    );

    // Copy names the Hermes hub, and nothing links out to clawhub.ai.
    fireEvent.click(screen.getByRole("button", { name: /Search Hermes skills hub for/i }));
    expect(callRpc).toHaveBeenCalledWith("skills.search", { query: "pdf", limit: 20 });
    expect(await screen.findByText("pdf")).toBeInTheDocument();
    expect(screen.getByText("official/pdf")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /ClawHub/i })).not.toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: /^Install$/i }));
    // The hub identifier IS the source; there is no registry name and no risk flag.
    await waitFor(() =>
      expect(callRpc).toHaveBeenLastCalledWith(
        "skills.install",
        { source: "official/pdf" },
        120_000,
      ),
    );
  });

  it("offers an explicit override when the install is blocked on trust, and acknowledges the risk", async () => {
    const callRpc = vi
      .fn()
      .mockResolvedValueOnce({ results: [RESULT] }) // search
      .mockRejectedValueOnce(new Error("ClawHub trust check failed — acknowledge the risk to proceed"))
      .mockResolvedValueOnce({ ok: true }); // install anyway
    render(<SkillHubSearch query="pdf" callRpc={callRpc} onInstalled={vi.fn()} />);

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
