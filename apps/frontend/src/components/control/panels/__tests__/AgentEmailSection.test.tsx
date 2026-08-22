import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

const mockGet = vi.fn();
const mockPatch = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: mockGet, patch: mockPatch }),
}));

import { AgentEmailSection } from "../AgentEmailSection";

// SWR has a global cache that persists across renders/tests — wrap each
// render in a fresh SWRConfig with `provider: () => new Map()` to isolate.
function renderSection(agentId = "agent_f") {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <AgentEmailSection agentId={agentId} />
    </SWRConfig>,
  );
}

const baseAddress = {
  address: "friday@acme.isol8.co",
  agent_id: "agent_f",
  verified_senders: ["o@x.y"],
  send_to_anyone: false,
  outbound_disabled: null as string | null,
  status: "active",
};

describe("AgentEmailSection", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPatch.mockReset();
    mockPatch.mockResolvedValue({});
  });

  it("renders the address and the verified-senders textarea seeded from the row", async () => {
    mockGet.mockResolvedValue({ addresses: [baseAddress] });
    renderSection();

    expect(await screen.findByText("friday@acme.isol8.co")).toBeInTheDocument();
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.value).toBe("o@x.y");
  });

  it("saves via PATCH /agent-mail/addresses/{agentId} with the edited senders and anyone flag", async () => {
    mockGet.mockResolvedValue({ addresses: [baseAddress] });
    renderSection();

    await screen.findByText("friday@acme.isol8.co");
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    expect(mockPatch).toHaveBeenCalledWith("/agent-mail/addresses/agent_f", {
      verified_senders: ["o@x.y"],
      send_to_anyone: false,
    });
  });

  it("shows the paused-outbound banner and re-enables via PATCH outbound_disabled: null", async () => {
    mockGet.mockResolvedValue({
      addresses: [{ ...baseAddress, outbound_disabled: "complaint" }],
    });
    renderSection();

    expect(
      await screen.findByText(/outbound email is paused/i),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /re-enable/i }));

    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    expect(mockPatch).toHaveBeenCalledWith("/agent-mail/addresses/agent_f", {
      verified_senders: ["o@x.y"],
      send_to_anyone: false,
      outbound_disabled: null,
    });
  });
});
