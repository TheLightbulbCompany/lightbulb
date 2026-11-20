// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import useSWR, { mutate as globalMutate } from "swr";

import { useResetSwrOnUserChange } from "./useResetSwrOnUserChange";

describe("useResetSwrOnUserChange", () => {
  it("drops cached reads when the person changes, never on the first sign-in", async () => {
    await globalMutate("viewer-read", "alice's rows", { revalidate: false });
    const { rerender } = renderHook(({ userId }: { userId: string | null | undefined }) => {
      useResetSwrOnUserChange(userId);
      return useSWR("viewer-read", null).data;
    }, { initialProps: { userId: undefined as string | null | undefined } });
    rerender({ userId: "alice" });
    expect(await globalMutate("viewer-read", (d: unknown) => d, { revalidate: false })).toBe("alice's rows");
    rerender({ userId: null });
    await waitFor(async () => expect(await globalMutate("viewer-read", (d: unknown) => d, { revalidate: false })).toBeUndefined());
  });
});
