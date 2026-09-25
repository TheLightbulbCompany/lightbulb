import { describe, test, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));

import { useProjectDetail } from "@/components/teams/projects/hooks/useProjectDetail";
import { useTeamsApi } from "@/hooks/useTeamsApi";

const mockUseTeamsApi = vi.mocked(useTeamsApi);

function makeRead(
  perPath: Record<string, { data?: unknown; isLoading?: boolean; error?: Error }>,
) {
  return vi.fn((path: string) => {
    const r = perPath[path] ?? { data: undefined, isLoading: false, error: null };
    return {
      data: r.data,
      isLoading: r.isLoading ?? false,
      error: r.error ?? null,
      mutate: vi.fn(),
      isValidating: false,
    };
  });
}

const baseApi = (read: ReturnType<typeof makeRead>) =>
  ({
    read,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }) as unknown as ReturnType<typeof useTeamsApi>;

// A real Paperclip project id — uuid-shaped, like every id this app links
// with produces. Kept short via this constant since most tests below only
// care that the id round-trips through the read paths, not its exact value.
const PID = "9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f";

describe("useProjectDetail", () => {
  test("returns undefined project + empty issues when reads return undefined", () => {
    mockUseTeamsApi.mockReturnValue(baseApi(makeRead({})));
    const { result } = renderHook(() => useProjectDetail("9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f"));
    expect(result.current.project).toBeUndefined();
    expect(result.current.issues).toEqual([]);
    expect(result.current.isLoading).toBe(false);
  });

  test("returns project + normalized issues envelope (the Issues tab)", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(
        makeRead({
          "/projects/9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f": {
            data: { id: "9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f", name: "Marketing" },
          },
          "/issues?project=9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f": {
            data: { issues: [{ id: "i_1", title: "Task", status: "todo" }] },
          },
        }),
      ),
    );
    const { result } = renderHook(() => useProjectDetail("9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f"));
    expect(result.current.project?.id).toBe("9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f");
    expect(result.current.project?.name).toBe("Marketing");
    expect(result.current.issues).toHaveLength(1);
  });

  test("normalizes issues returned as a bare array", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(
        makeRead({
          "/projects/9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f": { data: { id: "9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f", name: "x" } },
          "/issues?project=9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f": {
            data: [{ id: "i_1", title: "x", status: "todo" }],
          },
        }),
      ),
    );
    const { result } = renderHook(() => useProjectDetail("9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f"));
    expect(result.current.issues).toHaveLength(1);
  });

  test("isLoading reflects the project read; issuesLoading is separate", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(
        makeRead({
          "/projects/9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f": { isLoading: true },
          "/issues?project=9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f": { isLoading: false },
        }),
      ),
    );
    const { result } = renderHook(() => useProjectDetail("9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f"));
    expect(result.current.isLoading).toBe(true);
    expect(result.current.issuesLoading).toBe(false);
  });

  test("isError true + surfaces error when project read errors", () => {
    const err = new Error("boom");
    mockUseTeamsApi.mockReturnValue(
      baseApi(makeRead({ "/projects/9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f": { error: err } })),
    );
    const { result } = renderHook(() => useProjectDetail("9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f"));
    expect(result.current.isError).toBe(true);
    expect(result.current.error).toBe(err);
  });

  test("an issues-list error does NOT flip isError (only the project read gates it)", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(
        makeRead({
          "/projects/9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f": { data: { id: "9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f", name: "x" } },
          "/issues?project=9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f": { error: new Error("issues down") },
        }),
      ),
    );
    const { result } = renderHook(() => useProjectDetail("9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f"));
    expect(result.current.isError).toBe(false);
    expect(result.current.project?.id).toBe("9c3fa2b0-6e2a-4a1b-9b8b-1a2b3c4d5e6f");
  });

  test("calls read with the queryKeys-derived paths", () => {
    const read = makeRead({});
    mockUseTeamsApi.mockReturnValue(baseApi(read));
    renderHook(() => useProjectDetail(PID));
    expect(read).toHaveBeenCalledWith("/projects/" + PID);
    expect(read).toHaveBeenCalledWith("/issues?project=" + PID);
  });

  // A Paperclip-native `/<prefix>/projects/<urlKey>` link lands here with the
  // url-key as the projectId, not the uuid — upstream's GET /projects/:id has
  // no url-key fallback, so this hook must resolve it against the project
  // list before touching the detail/issues routes.
  describe("url-key resolution", () => {
    test("resolves a non-uuid projectId against the project list before fetching detail", () => {
      const read = makeRead({
        "/projects": { data: { projects: [{ id: PID, name: "Acme", urlKey: "acme-app" }] } },
        ["/projects/" + PID]: { data: { id: PID, name: "Acme" } },
        ["/issues?project=" + PID]: { data: { issues: [] } },
      });
      mockUseTeamsApi.mockReturnValue(baseApi(read));
      const { result } = renderHook(() => useProjectDetail("acme-app"));
      expect(result.current.project?.id).toBe(PID);
      // Never fired the detail read against the raw url-key.
      expect(read).not.toHaveBeenCalledWith("/projects/acme-app");
    });

    test("holds the detail fetch until the project list has settled", () => {
      const read = makeRead({ "/projects": { isLoading: true } });
      mockUseTeamsApi.mockReturnValue(baseApi(read));
      const { result } = renderHook(() => useProjectDetail("acme-app"));
      expect(read).not.toHaveBeenCalledWith(expect.stringMatching(/^\/projects\/acme-app/));
      expect(result.current.isLoading).toBe(true);
    });

    test("falls back to the raw id once the list has settled with no match", () => {
      const read = makeRead({
        "/projects": { data: { projects: [] } },
        "/projects/acme-app": { error: new Error("not found") },
      });
      mockUseTeamsApi.mockReturnValue(baseApi(read));
      const { result } = renderHook(() => useProjectDetail("acme-app"));
      expect(read).toHaveBeenCalledWith("/projects/acme-app");
      expect(result.current.isError).toBe(true);
    });

    test("skips the project-list read entirely for an already-uuid projectId", () => {
      const read = makeRead({ ["/projects/" + PID]: { data: { id: PID, name: "x" } } });
      mockUseTeamsApi.mockReturnValue(baseApi(read));
      renderHook(() => useProjectDetail(PID));
      expect(read).not.toHaveBeenCalledWith("/projects");
    });
  });
});
