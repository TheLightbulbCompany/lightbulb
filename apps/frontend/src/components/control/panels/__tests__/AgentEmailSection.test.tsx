import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { ApiError } from "@lightbulb/shared";

const mockGet = vi.fn();
const mockPatch = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: mockGet, patch: mockPatch }),
}));
const navigateShell = vi.fn();
vi.mock("@/lib/shellNav", () => ({ navigateShell: (url: string) => navigateShell(url) }));

let isAdmin = true;
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => isAdmin }));

import { AgentEmailSection } from "../AgentEmailSection";

// SWR has a global cache that persists across renders/tests — wrap each
// render in a fresh SWRConfig with `provider: () => new Map()` to isolate.
function renderSection() {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <AgentEmailSection />
    </SWRConfig>,
  );
}

const ADDR = "agents@acme.thelightbulb.company";
const baseAddress = {
  address: ADDR,
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
    isAdmin = true;
  });

  it("shows a member the settings read-only and says who can change them", async () => {
    isAdmin = false;
    mockGet.mockResolvedValue({ ...baseAddress, outbound_disabled: "complaint" });
    renderSection();

    await screen.findByText(ADDR);
    expect(screen.getByRole("textbox")).toBeDisabled();
    expect(screen.getByRole("checkbox")).toBeDisabled();
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /re-enable/i })).not.toBeInTheDocument();
    expect(screen.getByText("Only a workspace admin can change this.")).toBeInTheDocument();
  });

  it("says only an admin can change this when the server answers 403", async () => {
    mockGet.mockResolvedValue(baseAddress);
    mockPatch.mockRejectedValueOnce(new ApiError(403, { detail: "Organization admin access required" }));
    renderSection();

    await screen.findByText(ADDR);
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByText("Only a workspace admin can change this.")).toBeInTheDocument();
  });

  it("renders the team address and the verified-senders textarea seeded from the row", async () => {
    mockGet.mockResolvedValue(baseAddress);
    renderSection();

    expect(await screen.findByText(ADDR)).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith("/agent-mail/address");
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("o@x.y");
  });

  it("opens the Mail panel instead of listing mail inline", async () => {
    mockGet.mockResolvedValue(baseAddress);
    renderSection();

    await screen.findByText(ADDR);
    expect(mockGet).not.toHaveBeenCalledWith(expect.stringContaining("/agent-mail/inbox"));
    fireEvent.click(screen.getByRole("button", { name: "Open Mail" }));
    expect(navigateShell).toHaveBeenCalledWith("/chat?panel=mail");
  });

  it("copies the address and shows Copied", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    mockGet.mockResolvedValue(baseAddress);
    renderSection();

    await screen.findByText(ADDR);
    fireEvent.click(screen.getByRole("button", { name: /copy address/i }));
    expect(writeText).toHaveBeenCalledWith(ADDR);
    expect(await screen.findByText(/copied/i)).toBeInTheDocument();
  });

  it("does nothing, and does not throw, when the clipboard is unavailable", async () => {
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    mockGet.mockResolvedValue(baseAddress);
    renderSection();

    await screen.findByText(ADDR);
    fireEvent.click(screen.getByRole("button", { name: /copy address/i }));
    expect(screen.queryByText(/copied/i)).not.toBeInTheDocument();
  });

  it("saves via PATCH /agent-mail/address with the edited senders and anyone flag", async () => {
    mockGet.mockResolvedValue(baseAddress);
    renderSection();

    await screen.findByText(ADDR);
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    expect(mockPatch).toHaveBeenCalledWith("/agent-mail/address", {
      verified_senders: ["o@x.y"],
      send_to_anyone: false,
    });
  });

  it("shows the paused-outbound banner and re-enables via PATCH outbound_disabled: null ONLY", async () => {
    mockGet.mockResolvedValue({ ...baseAddress, outbound_disabled: "complaint" });
    renderSection();

    expect(await screen.findByText(/outbound email is paused/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /re-enable/i }));

    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    expect(mockPatch).toHaveBeenCalledWith("/agent-mail/address", { outbound_disabled: null });
  });

  // Regression: the reseed effect must depend on content, not the array reference, or Save's
  // own refetch clobbers in-progress edits.
  it("shows the server's updated senders after Save's refetch returns new data", async () => {
    mockGet
      .mockResolvedValueOnce(baseAddress)
      .mockResolvedValueOnce({ ...baseAddress, verified_senders: ["new@x.y"] });
    renderSection();

    await screen.findByText(ADDR);
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => {
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("new@x.y");
    });
  });

  it("disables the textarea and checkbox while a save is in flight", async () => {
    mockGet.mockResolvedValue(baseAddress);
    let resolvePatch: (value: unknown) => void = () => {};
    mockPatch.mockImplementation(() => new Promise((resolve) => { resolvePatch = resolve; }));
    renderSection();

    await screen.findByText(ADDR);
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(screen.getByRole("textbox")).toBeDisabled());
    expect(screen.getByRole("checkbox")).toBeDisabled();

    resolvePatch({});
    await waitFor(() => expect(screen.getByRole("textbox")).not.toBeDisabled());
  });

  it("surfaces the backend's bad_sender detail instead of the opaque 'API 400'", async () => {
    mockGet.mockResolvedValue(baseAddress);
    mockPatch.mockRejectedValueOnce(new ApiError(400, { detail: { status: "bad_sender", entry: "nope" } }));
    renderSection();

    await screen.findByText(ADDR);
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByText(/nope/i)).toBeInTheDocument();
  });

  it("shows the setting-up message on a 409 instead of crashing", async () => {
    mockGet.mockRejectedValue(new ApiError(409, { detail: { status: "no_company_name" } }));
    renderSection();

    expect(await screen.findByText(/being set up/i)).toBeInTheDocument();
  });
});
