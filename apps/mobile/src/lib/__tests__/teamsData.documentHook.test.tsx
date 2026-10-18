import { act, renderHook, waitFor } from "@testing-library/react-native";

jest.mock("expo-router", () => ({ useFocusEffect: jest.fn() }));
jest.mock("@clerk/expo", () => ({ useAuth: () => ({ getToken: jest.fn() }) }));

const mockPut = jest.fn();
const mockFetch = jest.fn();
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ put: mockPut, post: () => Promise.resolve({}) }),
  fetchJsonWithStatus: (...a: unknown[]) => mockFetch(...a),
}));

import { ApiError } from "@lightbulb/shared";
import { useTeamsIssue } from "../teamsData";

let revision = "rev_1";
beforeEach(() => {
  revision = "rev_1";
  mockPut.mockReset().mockResolvedValue({});
  mockFetch.mockImplementation(async (_c: unknown, url: string) => {
    if (url.endsWith("/documents"))
      return { status: 200, body: { documents: [{ id: "d", key: "plan", latestRevisionId: revision }] } };
    if (/\/issues\/i1$/.test(url)) return { status: 200, body: { id: "i1", title: "T" } };
    return { status: 200, body: [] };
  });
});

async function ready() {
  const hook = await renderHook(() => useTeamsIssue("i1"));
  // The screen loads through useFocusEffect (mocked out here), so load by hand.
  await act(async () => { await hook.result.current.reload(); });
  await waitFor(() => expect(hook.result.current.state.kind).toBe("ready"));
  return hook;
}

it("sends the revision the sheet was opened with, even after a reload moved it", async () => {
  const { result } = await ready();
  revision = "rev_2"; // an agent saved while the sheet was open
  await act(async () => { await result.current.reload(); });
  await act(async () => {
    await result.current.updateDocument("plan", "Plan", "body", "Edited by Ada", "rev_1");
  });
  expect(mockPut).toHaveBeenCalledWith(
    "/teams/issues/i1/documents/plan",
    { title: "Plan", format: "markdown", body: "body", changeSummary: "Edited by Ada", baseRevisionId: "rev_1" },
  );
});

it("reloads on a conflict and still rejects", async () => {
  const { result } = await ready();
  mockPut.mockRejectedValue(new ApiError(409, {}, "conflict"));
  revision = "rev_2";
  await act(async () => {
    await expect(result.current.updateDocument("plan", "P", "b", "Edited by You", "rev_1")).rejects.toBeInstanceOf(ApiError);
  });
  await waitFor(() => {
    const s = result.current.state;
    expect(s.kind === "ready" && s.documents[0].latestRevisionId).toBe("rev_2");
  });
});
