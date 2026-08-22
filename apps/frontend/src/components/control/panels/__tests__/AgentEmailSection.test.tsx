import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { ApiError } from "@isol8/shared";

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

  // Regression: the reseed effect used to depend on the raw
  // `verified_senders` array, which is a new reference on every SWR fetch —
  // so it re-ran (and clobbered in-progress edits) on every revalidation,
  // including the one `save()` triggers via `mutate()`. It must only reseed
  // when the server value actually changed.
  it("shows the server's updated senders after Save's refetch returns new data", async () => {
    mockGet
      .mockResolvedValueOnce({ addresses: [baseAddress] })
      .mockResolvedValueOnce({
        addresses: [{ ...baseAddress, verified_senders: ["new@x.y"] }],
      });
    renderSection();

    await screen.findByText("friday@acme.isol8.co");
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => {
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      expect(textarea.value).toBe("new@x.y");
    });
  });

  it("disables the textarea and checkbox while a save is in flight", async () => {
    mockGet.mockResolvedValue({ addresses: [baseAddress] });
    let resolvePatch: (value: unknown) => void = () => {};
    mockPatch.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePatch = resolve;
        }),
    );
    renderSection();

    await screen.findByText("friday@acme.isol8.co");
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(screen.getByRole("textbox")).toBeDisabled());
    expect(screen.getByRole("checkbox")).toBeDisabled();

    resolvePatch({});
    await waitFor(() => expect(screen.getByRole("textbox")).not.toBeDisabled());
  });

  it("surfaces the backend's bad_sender detail instead of the opaque 'API 400'", async () => {
    mockGet.mockResolvedValue({ addresses: [baseAddress] });
    mockPatch.mockRejectedValueOnce(
      new ApiError(400, { detail: { status: "bad_sender", entry: "nope" } }),
    );
    renderSection();

    await screen.findByText("friday@acme.isol8.co");
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByText(/nope/i)).toBeInTheDocument();
  });
});
