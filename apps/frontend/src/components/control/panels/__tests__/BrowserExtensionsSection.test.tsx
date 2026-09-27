// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import React from "react";
import { SWRConfig } from "swr";

const ID = "jblbcpkejogbghfdglhfjlplchcnmohm";
let listed: { slug: string; name: string; version: string; source: string; addedAt: string | null }[] = [];
const api = {
  get: vi.fn(async () => ({ extensions: listed, catalog: [{ slug: ID, name: "Supademo recorder" }] })),
  post: vi.fn(async () => {
    listed = [{ slug: ID, name: "Supademo", version: "7.5.16", source: "webstore", addedAt: null }];
    return { slug: ID, name: "Supademo", restarting: true };
  }),
  del: vi.fn(),
  upload: vi.fn(),
};
vi.mock("@/lib/api", () => ({ useApi: () => api }));

import { BrowserExtensionsSection } from "../BrowserExtensionsSection";

describe("BrowserExtensionsSection", () => {
  it("adds a pasted Web Store link for any member and says the browser restarts", async () => {
    render(<SWRConfig value={{ provider: () => new Map() }}><BrowserExtensionsSection /></SWRConfig>);
    const link = `https://chromewebstore.google.com/detail/supademo/${ID}`;
    fireEvent.change(await screen.findByLabelText("Chrome Web Store link or extension id"), { target: { value: link } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText(/Added Supademo\. The browser is restarting/)).toBeInTheDocument();
    expect(api.post).toHaveBeenCalledWith("/browser/extensions/webstore", { idOrUrl: link });
    expect(await screen.findByText("Supademo")).toBeInTheDocument();
    // An installed catalog extension is no longer offered.
    expect(screen.queryByRole("button", { name: "Add Supademo recorder" })).toBeNull();
  });
});
