// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { SWRConfig } from "swr";

let listed: { slug: string; name: string; version: string; source: string; addedAt: string | null }[] = [];
const api = {
  get: vi.fn(async () => ({ extensions: listed, catalog: [{ slug: "supademo", name: "Supademo recorder", version: "7.5.16" }] })),
  post: vi.fn(async (path: string) => {
    if (path.endsWith("/catalog/supademo")) listed = [{ slug: "supademo", name: "Supademo recorder", version: "7.5.16", source: "catalog", addedAt: null }];
    return {};
  }),
  del: vi.fn(),
  upload: vi.fn(),
};
vi.mock("@/lib/api", () => ({ useApi: () => api }));

import { BrowserExtensionsSection } from "../BrowserExtensionsSection";

describe("BrowserExtensionsSection", () => {
  it("adds a catalog extension, then offers a restart that stops before starting", async () => {
    render(<SWRConfig value={{ provider: () => new Map() }}><BrowserExtensionsSection canManage /></SWRConfig>);
    fireEvent.click(await screen.findByRole("button", { name: "Add Supademo recorder" }));
    expect(await screen.findByText("Supademo recorder")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add Supademo recorder" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Restart browser now" }));
    await waitFor(() => expect(api.post).toHaveBeenLastCalledWith("/browser/start", {}));
    expect(api.post.mock.calls.map((c) => c[0])).toEqual([
      "/browser/extensions/catalog/supademo",
      "/browser/stop",
      "/browser/start",
    ]);
  });

  it("shows non-admins an explanation instead of the list", () => {
    render(<BrowserExtensionsSection canManage={false} />);
    expect(screen.getByText(/Only organization admins/)).toBeInTheDocument();
  });
});
