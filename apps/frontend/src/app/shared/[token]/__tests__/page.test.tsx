import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { SWRConfig } from "swr";

vi.mock("next/navigation", () => ({ useParams: () => ({ token: "t" }) }));

import Page from "../page";

afterEach(() => vi.unstubAllGlobals());

const show = (status: number, body: unknown) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ status, ok: status < 300, json: async () => body }));
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <Page />
    </SWRConfig>,
  );
};

describe("shared session page", () => {
  it("lists the session and its tool calls", async () => {
    show(200, {
      session: { harness: "claude-code", computer: "mac", repo: "lightbulb", branch: "main", started: "2026-10-27T10:00:00Z", ended: null, prompt_tokens: 10, completion_tokens: 20 },
      tool_calls: [{ seq: 1, at: "x", tool: "Bash", summary: "ran tests", ok: true }],
    });
    expect(await screen.findByText("ran tests")).toBeInTheDocument();
    expect(screen.getByText("Bash")).toBeInTheDocument();
  });

  it("says so when the link is expired", async () => {
    show(404, {});
    expect(await screen.findByText(/expired or the session was deleted/)).toBeInTheDocument();
  });
});
