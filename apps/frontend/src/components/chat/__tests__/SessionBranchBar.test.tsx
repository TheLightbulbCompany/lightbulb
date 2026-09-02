import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const mutate = vi.fn().mockResolvedValue(undefined);
const useGatewayRpc = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (...args: unknown[]) => useGatewayRpc(...args),
}));

import { SessionBranchBar, type SessionBranch } from "@/components/chat/SessionBranchBar";

function branch(over: Partial<SessionBranch> = {}): SessionBranch {
  return {
    leafEntryId: "leaf-1",
    headline: "Original plan",
    messageCount: 12,
    active: true,
    ...over,
  };
}

function serve(branches: SessionBranch[] | undefined) {
  useGatewayRpc.mockReturnValue({
    data: branches ? { branches } : undefined,
    error: undefined,
    isLoading: false,
    mutate,
  });
}

beforeEach(() => {
  useGatewayRpc.mockReset();
  mutate.mockClear();
});

describe("SessionBranchBar", () => {
  it("renders nothing while the session has one branch — the ordinary case", () => {
    serve([branch()]);
    const { container } = render(
      <SessionBranchBar sessionKey="agent:a:u" onSwitch={vi.fn()} branchRefreshToken={0} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the container answered with no branches at all", () => {
    serve(undefined);
    const { container } = render(
      <SessionBranchBar sessionKey="agent:a:u" onSwitch={vi.fn()} branchRefreshToken={0} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("lists every branch with its headline and message count once a rewind created one", () => {
    serve([
      branch(),
      branch({ leafEntryId: "leaf-2", headline: "Second try", messageCount: 4, active: false }),
    ]);
    render(<SessionBranchBar sessionKey="agent:a:u" onSwitch={vi.fn()} branchRefreshToken={0} />);

    expect(screen.getByRole("button", { name: /Original plan · 12 messages/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Second try · 4 messages/ })).toBeInTheDocument();
  });

  it("marks the active branch and refuses to switch to it", () => {
    serve([
      branch(),
      branch({ leafEntryId: "leaf-2", headline: "Second try", messageCount: 4, active: false }),
    ]);
    render(<SessionBranchBar sessionKey="agent:a:u" onSwitch={vi.fn()} branchRefreshToken={0} />);

    const active = screen.getByRole("button", { name: /Original plan/ });
    expect(active).toHaveAttribute("aria-current", "true");
    expect(active).toBeDisabled();
    expect(screen.getByRole("button", { name: /Second try/ })).toBeEnabled();
  });

  it("switches to the clicked branch tip", async () => {
    const onSwitch = vi.fn().mockResolvedValue(true);
    serve([
      branch(),
      branch({ leafEntryId: "leaf-2", headline: "Second try", messageCount: 4, active: false }),
    ]);
    render(
      <SessionBranchBar sessionKey="agent:a:u" onSwitch={onSwitch} branchRefreshToken={0} />,
    );

    await userEvent.click(screen.getByRole("button", { name: /Second try/ }));
    expect(onSwitch).toHaveBeenCalledWith("leaf-2");
  });

  it("disables switching while a run is active", () => {
    serve([
      branch(),
      branch({ leafEntryId: "leaf-2", headline: "Second try", messageCount: 4, active: false }),
    ]);
    render(
      <SessionBranchBar sessionKey="agent:a:u" onSwitch={vi.fn()} branchRefreshToken={0} disabled />,
    );
    expect(screen.getByRole("button", { name: /Second try/ })).toBeDisabled();
  });

  it("re-lists when branchRefreshToken advances after a rewind", async () => {
    serve([branch()]);
    const { rerender } = render(
      <SessionBranchBar sessionKey="agent:a:u" onSwitch={vi.fn()} branchRefreshToken={0} />,
    );
    expect(mutate).not.toHaveBeenCalled();

    rerender(
      <SessionBranchBar sessionKey="agent:a:u" onSwitch={vi.fn()} branchRefreshToken={1} />,
    );
    await waitFor(() => expect(mutate).toHaveBeenCalled());
  });

  it("reads no branches at all without a session key", () => {
    serve(undefined);
    render(<SessionBranchBar sessionKey={null} onSwitch={vi.fn()} branchRefreshToken={0} />);
    expect(useGatewayRpc).toHaveBeenCalledWith(null, undefined, expect.anything());
  });
});
