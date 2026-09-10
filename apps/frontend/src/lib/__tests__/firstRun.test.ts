import { describe, expect, it, vi } from "vitest";
import { readFirstRun, writeFirstRun } from "@/lib/firstRun";

function fakeUser(meta: Record<string, unknown>) {
  return {
    unsafeMetadata: meta,
    update: vi.fn().mockResolvedValue(undefined),
  } as never;
}

describe("readFirstRun", () => {
  it("returns empty object when absent", () => {
    expect(readFirstRun(fakeUser({}))).toEqual({});
    expect(readFirstRun(null)).toEqual({});
  });

  it("returns the stored meta", () => {
    const u = fakeUser({ firstRun: { briefSubmitted: true, path: "grow" } });
    expect(readFirstRun(u).briefSubmitted).toBe(true);
    expect(readFirstRun(u).path).toBe("grow");
  });
});

describe("writeFirstRun", () => {
  it("merges the patch and preserves sibling metadata keys", async () => {
    const u = fakeUser({
      getStarted: { seen: true },
      firstRun: { briefSubmitted: true },
    });
    await writeFirstRun(u, { path: "grow" });
    const arg = (u as { update: ReturnType<typeof vi.fn> }).update.mock.calls[0][0];
    expect(arg.unsafeMetadata.getStarted).toEqual({ seen: true });
    expect(arg.unsafeMetadata.firstRun).toEqual({ briefSubmitted: true, path: "grow" });
  });
});
