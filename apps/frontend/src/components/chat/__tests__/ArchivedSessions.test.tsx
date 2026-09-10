import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

const callRpc = vi.fn().mockResolvedValue({});
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: vi.fn(),
  useRuntimeRpcMutation: (): typeof callRpc => callRpc,
}));

import { ArchivedSessions } from "../ArchivedSessions";

function renderDrawer(onDelete = vi.fn()) {
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <ArchivedSessions agentId="a" userId="u" onDelete={onDelete} />
    </SWRConfig>,
  );
  return onDelete;
}

describe("ArchivedSessions", () => {
  beforeEach(() => {
    callRpc.mockReset().mockImplementation(async (method: string) =>
      method === "sessions.list"
        ? {
            sessions: [
              {
                key: "agent:a:u:11111111-1111-4111-8111-111111111111",
                derivedTitle: "old thread",
                updatedAt: 1,
              },
            ],
          }
        : {},
    );
  });

  it("fetches nothing until it is opened", async () => {
    renderDrawer();
    // The whole point of the collapsed default: a second sessions.list with
    // transcript projections must not ride along on every rail render.
    await waitFor(() => expect(screen.getByRole("button", { name: /Archived/ })).toBeTruthy());
    expect(callRpc).not.toHaveBeenCalled();
  });

  it("lists archived rows on open, with restore and permanent delete", async () => {
    const onDelete = renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: /Archived/ }));
    await waitFor(() => expect(screen.getByText("Old thread")).toBeTruthy());
    const [, params] = callRpc.mock.calls.find(([m]) => m === "sessions.list") as [
      string,
      Record<string, unknown>,
    ];
    expect(params.archived).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Restore Old thread" }));
    await waitFor(() =>
      expect(callRpc).toHaveBeenCalledWith("sessions.patch", {
        key: "agent:a:u:11111111-1111-4111-8111-111111111111",
        archived: false,
      }),
    );

    // Delete stays the caller's confirm dialog — the drawer never deletes
    // straight from a click.
    fireEvent.click(screen.getByRole("button", { name: "Delete Old thread permanently" }));
    expect(onDelete).toHaveBeenCalledWith(expect.objectContaining({ label: "Old thread" }));
  });

  it("does not offer selection of an archived row", async () => {
    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: /Archived/ }));
    await waitFor(() => expect(screen.getByText("Old thread")).toBeTruthy());
    // Restore it first — an archived session the rail does not list must not
    // become the chat's active session.
    expect(screen.queryByRole("button", { name: "Old thread" })).toBeNull();
  });
});
