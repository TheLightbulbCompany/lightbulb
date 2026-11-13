// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { ApiError } from "@/lib/api";
import { JoinLinkCopyError } from "@/lib/clipboard";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const copy = vi.hoisted(() => ({ result: true }));
const capture = vi.hoisted(() => vi.fn());
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ orgId: "org_1" }) }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<Record<string, unknown>>()), useApi: () => api }));
vi.mock("@/lib/analytics", () => ({ capture }));
vi.mock("@/lib/clipboard", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  copyTextWhenReady: async (text: Promise<string>) => { await text.catch(() => undefined); return copy.result; },
}));

import { JOIN_LINK_COPIED, joinLinkNote, useCopyJoinLink } from "../useCopyJoinLink";

const run = (source = "test") => renderHook(() => useCopyJoinLink()).result.current(source);

beforeEach(() => { api.get.mockReset(); api.post.mockReset(); capture.mockReset(); copy.result = true; });

it("copies an existing link without minting, and records where it came from", async () => {
  api.get.mockResolvedValue({ exists: true, join_url: "https://x/join/abc" });
  await run("workspace_header");
  expect(api.post).not.toHaveBeenCalled();
  expect(capture).toHaveBeenCalledWith("join_link_copied", { source: "workspace_header", workspace_id: "org_1" });
});

it("mints a link when there is none", async () => {
  api.get.mockResolvedValue({ exists: false });
  api.post.mockResolvedValue({ join_url: "https://x/join/new" });
  await run();
  expect(api.post).toHaveBeenCalledWith("/orgs/org_1/join-link", {});
});

it("tells a non-admin to ask an admin", async () => {
  api.get.mockRejectedValue(new ApiError(403, { detail: "admin only" }));
  const error = await run().catch((e: unknown) => e);
  expect(joinLinkNote(error)).toBe("This workspace's join link is turned off. Ask a workspace admin for a new one.");
  expect(capture).not.toHaveBeenCalled();
});

it("never rotates a link it cannot show", async () => {
  api.get.mockResolvedValue({ exists: true, join_url: null });
  const error = await run().catch((e: unknown) => e);
  expect(error).toBeInstanceOf(JoinLinkCopyError);
  expect(api.post).not.toHaveBeenCalled();
  expect(joinLinkNote(error)).toMatch(/^This workspace's link can't be shown here/);
});

it("says so when the clipboard refuses", async () => {
  api.get.mockResolvedValue({ exists: true, join_url: "https://x/join/abc" });
  copy.result = false;
  const error = await run().catch((e: unknown) => e);
  expect(joinLinkNote(error)).toBe("Couldn't copy automatically. Try again, or ask a workspace admin for the link.");
  expect(JOIN_LINK_COPIED).toBe("Copied. Anyone who opens it and signs in joins this workspace.");
});
