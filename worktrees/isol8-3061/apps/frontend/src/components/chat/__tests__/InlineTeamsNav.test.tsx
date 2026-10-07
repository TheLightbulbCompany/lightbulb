import { describe, it, expect } from "vitest";
import { renderHook } from "@testing-library/react";
import {
  InlineTeamsNavProvider,
  useInlineTeamsNav,
} from "../InlineTeamsNav";

describe("useInlineTeamsNav", () => {
  it("returns null when there is no provider", () => {
    const { result } = renderHook(() => useInlineTeamsNav());
    expect(result.current).toBeNull();
  });

  it("returns the provided value inside the provider", () => {
    const openDetail = () => {};
    const leaveToList = () => {};
    const value = { openDetail, leaveToList };
    const { result } = renderHook(() => useInlineTeamsNav(), {
      wrapper: ({ children }) => (
        <InlineTeamsNavProvider value={value}>
          {children}
        </InlineTeamsNavProvider>
      ),
    });
    expect(result.current).toBe(value);
    expect(result.current?.openDetail).toBe(openDetail);
  });
});
