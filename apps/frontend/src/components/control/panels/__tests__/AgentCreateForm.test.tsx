import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AgentCreateForm } from "../AgentCreateForm";

const callRpc = vi.fn();

vi.mock("@/hooks/useRuntimeRpc", () => ({ useRuntimeRpcMutation: () => callRpc }));
vi.mock("@/components/control/agents/AgentPaperclipFields", () => ({
  AgentPaperclipFields: () => null,
}));

describe("AgentCreateForm", () => {
  beforeEach(() => callRpc.mockReset());

  it("renders a former agent-limit error as an ordinary create failure", async () => {
    callRpc.mockRejectedValue(
      Object.assign(new Error("temporary create failure"), { code: "agent_budget_exceeded" }),
    );
    render(
      <AgentCreateForm existingNames={[]} onCreated={() => {}} onCancel={() => {}} />,
    );

    fireEvent.change(screen.getByPlaceholderText("e.g. Research Assistant"), {
      target: { value: "Helper" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByText("temporary create failure")).toBeInTheDocument();
    await waitFor(() => expect(callRpc).toHaveBeenCalledOnce());
    expect(screen.queryByRole("button", { name: /upgrade to ultra/i })).not.toBeInTheDocument();
  });

  it("does not send a workspace path — the backend owns the layout", async () => {
    callRpc.mockResolvedValue({});
    render(
      <AgentCreateForm existingNames={[]} onCreated={() => {}} onCancel={() => {}} />,
    );

    fireEvent.change(screen.getByPlaceholderText("e.g. Research Assistant"), {
      target: { value: "Ada" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(callRpc).toHaveBeenCalledOnce());
    expect(callRpc).toHaveBeenCalledWith(
      "agents.create",
      expect.not.objectContaining({ workspace: expect.anything() }),
    );
    expect(JSON.stringify(callRpc.mock.calls)).not.toContain(".openclaw");
  });
});
