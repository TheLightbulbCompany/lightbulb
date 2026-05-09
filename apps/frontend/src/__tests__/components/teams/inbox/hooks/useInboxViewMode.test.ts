import { describe, it, expect, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useInboxViewMode } from "@/components/teams/inbox/hooks/useInboxViewMode";

describe("useInboxViewMode", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("defaults to 'list' with no stored value", () => {
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    expect(result.current.viewMode).toBe("list");
  });

  it("reads a stored value", () => {
    window.localStorage.setItem("paperclip:inbox:co-1:view-mode", "board");
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    expect(result.current.viewMode).toBe("board");
  });

  it("writes on setViewMode", () => {
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    act(() => result.current.setViewMode("board"));
    expect(result.current.viewMode).toBe("board");
    expect(window.localStorage.getItem("paperclip:inbox:co-1:view-mode")).toBe("board");
  });

  it("scopes per companyId", () => {
    window.localStorage.setItem("paperclip:inbox:co-1:view-mode", "board");
    const { result } = renderHook(() => useInboxViewMode("co-2"));
    expect(result.current.viewMode).toBe("list");
  });

  it("returns 'list' with null companyId without touching localStorage", () => {
    const { result } = renderHook(() => useInboxViewMode(null));
    expect(result.current.viewMode).toBe("list");
    act(() => result.current.setViewMode("board"));
    expect(window.localStorage.length).toBe(0);
  });

  it("ignores unknown stored values", () => {
    window.localStorage.setItem("paperclip:inbox:co-1:view-mode", "garbage");
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    expect(result.current.viewMode).toBe("list");
  });

  it("re-reads storage when companyId changes", () => {
    window.localStorage.setItem("paperclip:inbox:co-1:view-mode", "board");
    const { result, rerender } = renderHook(
      ({ id }) => useInboxViewMode(id),
      { initialProps: { id: "co-2" as string | null } },
    );
    expect(result.current.viewMode).toBe("list");
    rerender({ id: "co-1" });
    expect(result.current.viewMode).toBe("board");
  });
});
