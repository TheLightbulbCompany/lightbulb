import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { ApiError } from "@lightbulb/shared";

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
  address: "friday@acme.thelightbulb.company",
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

    expect(await screen.findByText("friday@acme.thelightbulb.company")).toBeInTheDocument();
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.value).toBe("o@x.y");
  });

  it("saves via PATCH /agent-mail/addresses/{agentId} with the edited senders and anyone flag", async () => {
    mockGet.mockResolvedValue({ addresses: [baseAddress] });
    renderSection();

    await screen.findByText("friday@acme.thelightbulb.company");
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    expect(mockPatch).toHaveBeenCalledWith("/agent-mail/addresses/agent_f", {
      verified_senders: ["o@x.y"],
      send_to_anyone: false,
    });
  });

  it("defaults Open email to ON for an agent with no row yet, and the owner can still turn it off", async () => {
    mockGet.mockResolvedValue({ addresses: [] });
    renderSection();

    const toggle = (await screen.findByRole("checkbox")) as HTMLInputElement;
    expect(toggle.checked).toBe(true);

    fireEvent.click(toggle);
    expect(toggle.checked).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    // No row and a blank list: `verified_senders` is left out so the minted row keeps its
    // owner-seeded allowlist instead of being overwritten with [].
    expect(mockPatch).toHaveBeenCalledWith("/agent-mail/addresses/agent_f", { send_to_anyone: false });
  });

  it("shows an explicitly closed row as OFF", async () => {
    mockGet.mockResolvedValue({ addresses: [baseAddress] });
    renderSection();

    await screen.findByText("friday@acme.thelightbulb.company");
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
  });

  it("shows the paused-outbound banner and re-enables via PATCH outbound_disabled: null ONLY", async () => {
    // Re-enable must not also persist unsaved edits — it PATCHes exactly
    // { outbound_disabled: null }, not save()'s full body.
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

    await screen.findByText("friday@acme.thelightbulb.company");
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => {
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      expect(textarea.value).toBe("new@x.y");
    });
  });

  // Regression: a deep-link retarget (AgentsPanel changes the selected agent
  // without resetting tabs) can move from one agent with no row to ANOTHER
  // agent with no row — same `undefined` derived values, so the reseed
  // effect didn't re-fire on `row?.address`/`sendersFromRow`/`anyoneFromRow`
  // alone, and the first agent's unsaved textarea text carried over.
  it("clears unsaved textarea text when the deep link retargets to a different agent with no row", async () => {
    mockGet.mockResolvedValue({ addresses: [baseAddress] }); // neither agent_a nor agent_b has a row here
    const { rerender } = renderSection("agent_a");

    const textarea = (await screen.findByRole("textbox")) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "typed@x.y" } });
    expect(textarea.value).toBe("typed@x.y");

    rerender(
      <SWRConfig value={{ provider: () => new Map() }}>
        <AgentEmailSection agentId="agent_b" />
      </SWRConfig>,
    );

    await waitFor(() => {
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
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

    await screen.findByText("friday@acme.thelightbulb.company");
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

    await screen.findByText("friday@acme.thelightbulb.company");
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByText(/nope/i)).toBeInTheDocument();
  });
});
