// Endpoint/body-shape coverage for useBrainFiles.ts against the backend
// contract in routers/brain.py — the exact things tsc can't catch (they're
// all `string` / `api.put(url, unknown)`), so a wrong path or body shape
// would silently 404/422 in prod with no test failing today.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useBrainTree, useBrainFile, useBrainMutations } from "../useBrainFiles";

const api = { get: vi.fn(), put: vi.fn(), del: vi.fn() };
vi.mock("@/lib/api", () => ({ useApi: () => api }));

// Fresh SWR cache per test — no cross-test cache bleed.
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
);

beforeEach(() => {
  vi.clearAllMocks();
});

describe("useBrainTree", () => {
  it("GETs /container/brain/tree", async () => {
    api.get.mockResolvedValue({ files: [] });
    const { result } = renderHook(() => useBrainTree(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(api.get).toHaveBeenCalledWith("/container/brain/tree");
  });
});

describe("useBrainFile", () => {
  it("GETs /container/brain/file?path=... with the path URI-encoded", async () => {
    api.get.mockResolvedValue({
      name: "a.md",
      path: "main/a b.md",
      size: 1,
      modified_at: 1,
      content: "x",
      binary: false,
      mime_type: "text/markdown",
    });
    const { result } = renderHook(() => useBrainFile("main/a b.md"), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(api.get).toHaveBeenCalledWith("/container/brain/file?path=main%2Fa%20b.md");
    expect(result.current.key).toBe("/container/brain/file?path=main%2Fa%20b.md");
  });

  it("does not fetch when path is null (null-key gating)", () => {
    renderHook(() => useBrainFile(null), { wrapper });
    expect(api.get).not.toHaveBeenCalled();
  });
});

describe("useBrainMutations", () => {
  it("saves via PUT /container/brain/file with body {path, content}", async () => {
    api.put.mockResolvedValue({ path: "main/a.md", size: 5, modified_at: 123 });
    const { result } = renderHook(() => useBrainMutations(), { wrapper });
    await result.current.saveBrainFile("main/a.md", "hello");
    expect(api.put).toHaveBeenCalledWith("/container/brain/file", {
      path: "main/a.md",
      content: "hello",
    });
  });

  it("deletes via DELETE /container/brain/file?path=... with the path URI-encoded", async () => {
    api.del.mockResolvedValue(null);
    const { result } = renderHook(() => useBrainMutations(), { wrapper });
    await result.current.deleteBrainFile("main/a b.md");
    expect(api.del).toHaveBeenCalledWith("/container/brain/file?path=main%2Fa%20b.md");
  });
});
