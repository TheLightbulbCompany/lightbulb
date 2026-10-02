// @vitest-environment jsdom
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { SWRConfig } from "swr";
import { describe, expect, it, vi } from "vitest";

import { documentPreviewText, extractDocumentLinks, useDocumentPreview } from "./documentLinks";
import { extractPreviewUrls } from "./linkPreview";

describe("extractDocumentLinks", () => {
  it("finds relative and absolute document links, de-duplicated, capped at two", () => {
    const text = [
      "Done: [Plan](/HAB/issues/HAB-12#document-video-plan)",
      "again https://dev.thelightbulb.company/HAB/issues/HAB-12#document-video-plan",
      "and <https://thelightbulb.company/HAB/issues/HAB-13#document-research>",
      "and /HAB/issues/HAB-14#document-plan",
    ].join("\n");
    expect(extractDocumentLinks(text)).toEqual([
      { issueId: "HAB-12", key: "video-plan" },
      { issueId: "HAB-13", key: "research" },
    ]);
  });

  it("ignores plain task links, comment anchors and paths buried in another URL", () => {
    expect(
      extractDocumentLinks("/HAB/issues/HAB-12 /HAB/issues/HAB-12#comment-abc https://x.com/a/HAB/issues/HAB-1#document-plan"),
    ).toEqual([]);
  });

  it("keeps document links out of the web link previews", () => {
    expect(extractPreviewUrls("https://thelightbulb.company/HAB/issues/HAB-12#document-plan https://a.com")).toEqual(["https://a.com"]);
  });
});

describe("documentPreviewText", () => {
  it("strips markup, skips a heading that repeats the title, and keeps the first lines", () => {
    const body = "# Video plan\n\n**Habit-tracking** app, [4.7★](https://x.com).\n\n| a | b |\n|---|---|\n- one\n- two";
    expect(documentPreviewText(body, "Video plan")).toBe("Habit-tracking app, 4.7★. a b one");
  });
});

const wrapper = ({ children }: { children: ReactNode }) => <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>;

describe("useDocumentPreview", () => {
  it("reads the task and the document through the Teams BFF", async () => {
    const get = vi.fn((path: string) =>
      Promise.resolve(path.includes("/documents/") ? { id: "d1", key: "plan", title: "Plan" } : { id: "i1", identifier: "HAB-12", title: "Task" }),
    );
    const { result } = renderHook(() => useDocumentPreview({ get }, { issueId: "HAB-12", key: "plan" }), { wrapper });
    await waitFor(() => expect(result.current?.document.title).toBe("Plan"));
    expect(result.current?.issue.identifier).toBe("HAB-12");
    expect(get).toHaveBeenCalledWith("/teams/issues/HAB-12");
    expect(get).toHaveBeenCalledWith("/teams/issues/HAB-12/documents/plan");
  });

  it("returns undefined when either read fails", async () => {
    const get = vi.fn().mockRejectedValue(new Error("404"));
    const { result } = renderHook(() => useDocumentPreview({ get }, { issueId: "X-1", key: "plan" }), { wrapper });
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(result.current).toBeUndefined();
  });
});
