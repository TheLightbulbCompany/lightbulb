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
    window.localStorage.setItem("isol8:teams:inbox-view-mode:co-1", "board");
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    expect(result.current.viewMode).toBe("board");
  });

  it("writes on setViewMode", () => {
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    act(() => result.current.setViewMode("board"));
    expect(result.current.viewMode).toBe("board");
    expect(window.localStorage.getItem("isol8:teams:inbox-view-mode:co-1")).toBe("board");
  });

  it("scopes per companyId", () => {
    window.localStorage.setItem("isol8:teams:inbox-view-mode:co-1", "board");
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
    window.localStorage.setItem("isol8:teams:inbox-view-mode:co-1", "garbage");
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    expect(result.current.viewMode).toBe("list");
  });
});
