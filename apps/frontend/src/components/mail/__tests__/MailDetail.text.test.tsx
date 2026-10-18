import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { SWRConfig } from "swr";

const mockGet = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: mockGet, post: vi.fn() }) }));

import { MailDetail } from "../MailDetail";
import type { MailMessage } from "../mailModel";

const base: MailMessage = {
  key: "k1", direction: "in", from: "+15551230000", to: "agents@acme.thelightbulb.company", subject: "",
  at: "2026-10-17T00:00:00Z", agent_id: null, text: "hi", channel: "text",
};

function show(message: MailMessage) {
  return render(<SWRConfig value={{ provider: () => new Map() }}>
    <MailDetail message={message} at={0} agentName={null} orgId="org_1" />
  </SWRConfig>);
}

describe("MailDetail text", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockGet.mockResolvedValue({ number: "+15559990000", agentId: "a1", businessPhone: "", humanThreads: ["+15551230000"] });
  });

  it("renders the reply box only for inbound text messages", async () => {
    show(base);
    expect(screen.getByRole("textbox")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Hand back" })).toBeInTheDocument();
  });

  it("renders no reply box for mail or an outbound text", async () => {
    const { unmount } = show({ ...base, channel: "mail" });
    expect(screen.queryByRole("textbox")).toBeNull();
    unmount();
    show({ ...base, direction: "out" });
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});
