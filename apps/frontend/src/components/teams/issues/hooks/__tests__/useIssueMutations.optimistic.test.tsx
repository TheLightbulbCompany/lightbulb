import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import useSWR, { SWRConfig, useSWRConfig } from "swr";
import type { Issue } from "@/components/teams/shared/types";

// Real SWR cache (fresh per test), so the optimistic write, the rollback and
// the in-flight-refetch guard are SWR's own behaviour, not a mock's.
const patch = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => {
    const { mutate } = useSWRConfig();
    return { post: vi.fn(), patch, del: vi.fn(), mutate };
  },
}));

import { useIssueMutations } from "../useIssueMutations";

const KEY = "/teams/issues";
let server: Record<string, Issue["status"]> = {};
const fetcher = async () => ({
  issues: Object.entries(server).map(
    ([id, status]) => ({ id, title: id, status, priority: null }) as Issue,
  ),
});

function deferred() {
  let finish!: () => void;
  const promise = new Promise((r) => (finish = () => r({})));
  return { promise, finish };
}

function setup() {
  server = { a: "todo", b: "todo" };
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>
  );
  return renderHook(
    () => ({ list: useSWR(KEY, fetcher), mutations: useIssueMutations() }),
    { wrapper },
  );
}

const statusOf = (r: ReturnType<typeof setup>["result"], id = "a") =>
  r.current.list.data?.issues.find((i) => i.id === id)?.status;

describe("useIssueMutations.updateInList", () => {
  it("moves the card at once and keeps it there through a refetch that lands mid-flight", async () => {
    const { result } = setup();
    await waitFor(() => expect(statusOf(result)).toBe("todo"));

    let finish!: () => void;
    patch.mockReturnValueOnce(new Promise((r) => (finish = () => r({ id: "a" }))));
    let done!: Promise<void>;
    act(() => {
      done = result.current.mutations.updateInList("/issues", "a", { status: "done" });
    });
    expect(statusOf(result)).toBe("done");

    // A refresh fires while the PATCH is still out; the server still says todo.
    await act(async () => {
      await result.current.list.mutate();
    });
    expect(statusOf(result)).toBe("done");

    server.a = "done";
    await act(async () => {
      finish();
      await done;
    });
    await waitFor(() => expect(statusOf(result)).toBe("done"));
  });

  it("rolls the card back and rejects when the PATCH fails", async () => {
    const { result } = setup();
    await waitFor(() => expect(statusOf(result)).toBe("todo"));

    patch.mockRejectedValueOnce(new Error("nope"));
    let outcome: unknown;
    await act(async () => {
      outcome = await result.current.mutations
        .updateInList("/issues", "a", { status: "done" })
        .catch((e) => e);
    });
    expect(outcome).toBeInstanceOf(Error);
    expect(statusOf(result)).toBe("todo");
  });

  it("keeps two in-flight moves, whichever PATCH returns first", async () => {
    const { result } = setup();
    await waitFor(() => expect(statusOf(result)).toBe("todo"));

    const first = deferred();
    const second = deferred();
    patch.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    let moves!: Promise<void>[];
    act(() => {
      moves = [
        result.current.mutations.updateInList("/issues", "a", { status: "done" }),
        result.current.mutations.updateInList("/issues", "b", { status: "blocked" }),
      ];
    });
    // The second move stacks on the first instead of undoing it.
    expect(statusOf(result, "a")).toBe("done");
    expect(statusOf(result, "b")).toBe("blocked");

    // The second PATCH lands first; the server has not applied the first yet.
    server.b = "blocked";
    await act(async () => {
      second.finish();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(statusOf(result, "a")).toBe("done");

    server.a = "done";
    await act(async () => {
      first.finish();
      await Promise.all(moves);
    });
    await waitFor(() => {
      expect(statusOf(result, "a")).toBe("done");
      expect(statusOf(result, "b")).toBe("blocked");
    });
  });
});
