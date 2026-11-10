import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";

const get = vi.fn();
const post = vi.fn();
const put = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post, put }) }));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn(), notifyError: vi.fn() }));
vi.mock("@/lib/externalAuth", () => ({ isTauri: () => false, openExternalUrl: vi.fn() }));

import { LinkWalletSection } from "../LinkWalletSection";

const wrap = (ui: React.ReactElement) =>
  render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{ui}</SWRConfig>);


describe("Link wallet UI is dark when the feature is off", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
  });

  it("hides the Connections card when the status route 404s", async () => {
    get.mockRejectedValue(new Error("404"));
    const { container } = wrap(<LinkWalletSection canManage />);
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("never asks, and never shows, for a non-admin", () => {
    const { container } = wrap(<LinkWalletSection canManage={false} />);
    expect(get).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });

  it("shows Connect for an admin when the feature is on", async () => {
    get.mockResolvedValue({ connected: false, configured: true });
    wrap(<LinkWalletSection canManage />);
    expect(await screen.findByRole("button", { name: /connect link wallet/i })).toBeEnabled();
  });


});
