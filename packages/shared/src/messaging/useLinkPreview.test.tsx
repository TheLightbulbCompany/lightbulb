// @vitest-environment jsdom
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { SWRConfig } from "swr";
import { describe, expect, it, vi } from "vitest";

import { useLinkPreview } from "./linkPreview";

const wrapper = ({ children }: { children: ReactNode }) => <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>;

describe("useLinkPreview", () => {
  it("fetches the preview once per URL and returns it", async () => {
    const get = vi.fn().mockResolvedValue({ url: "https://a.com", title: "A" });
    const { result, rerender } = renderHook(() => useLinkPreview({ get }, "https://a.com"), { wrapper });
    await waitFor(() => expect(result.current?.title).toBe("A"));
    rerender();
    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith("/messaging/link-preview?url=https%3A%2F%2Fa.com");
  });

  it("returns undefined on failure and does not retry", async () => {
    const get = vi.fn().mockRejectedValue(new Error("404"));
    const { result } = renderHook(() => useLinkPreview({ get }, "https://b.com"), { wrapper });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    expect(result.current).toBeUndefined();
  });
});
