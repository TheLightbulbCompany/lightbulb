// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const get = vi.fn();
const dock = vi.hoisted(() => ({ openPanelIds: [] as string[], show: vi.fn(() => true) }));
const apiClient = { get };
vi.mock("@/lib/api", () => ({ useApi: () => apiClient }));
vi.mock("@/components/workspace/workspaceController", () => ({
  BROWSER_PANEL_ID: "browser",
  showBrowserBesideChat: dock.show,
  useWorkspaceState: () => ({ openPanelIds: dock.openPanelIds }),
}));
vi.mock("@/components/workspace/panels/dcvWebKitShim", () => ({ ensureDcvMediaShim: () => true }));
vi.mock("@/components/workspace/panels/DirectDcvLiveView", () => ({
  DirectDcvLiveView: ({ signedUrl }: { signedUrl: string }) => <div data-testid="thumbnail" data-url={signedUrl} />,
}));

import { browsingSite } from "@lightbulb/shared";
import { AUTO_OPENED_KEY, BrowserRunPreview } from "../BrowserRunPreview";

const tool = (name: string, args: unknown = {}) => ({ id: name, name, args, result: null, status: "done" as const });

async function flush(): Promise<void> {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

beforeEach(() => {
  get.mockReset();
  dock.show.mockClear();
  dock.openPanelIds = [];
  window.sessionStorage.clear();
});

describe("browsingSite", () => {
  it("is undefined until the run touches the browser, then names the last site it opened", () => {
    expect(browsingSite({ tools: [tool("terminal")] })).toBeUndefined();
    expect(browsingSite({ tools: [tool("browser_snapshot")] })).toBeNull();
    expect(browsingSite({ tools: [
      tool("browser_navigate", { url: "https://www.example.com/a" }),
      tool("browser_click"),
      tool("browser_navigate", { url: "https://supademo.com/x" }),
    ] })).toBe("supademo.com");
  });
});

describe("BrowserRunPreview", () => {
  it("shows a live thumbnail and opens the panel beside the chat once per tab session", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1" });
    const first = render(<BrowserRunPreview agentName="Friday" site="supademo.com" />);
    await flush();
    expect(screen.getByText("Friday is browsing supademo.com")).toBeInTheDocument();
    expect(screen.getByTestId("thumbnail")).toHaveAttribute("data-url", "https://signed");
    expect(dock.show).toHaveBeenCalledTimes(1);
    expect(dock.show).toHaveBeenCalledWith(); // beside the chat, never as a tab over it
    expect(window.sessionStorage.getItem(AUTO_OPENED_KEY)).toBe("1");
    first.unmount();

    render(<BrowserRunPreview agentName="Friday" site={null} />); // the next run: no second auto-open
    await flush();
    expect(dock.show).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /open the shared browser/i }));
    expect(dock.show).toHaveBeenLastCalledWith({ focus: true, asTabWhenNarrow: true });
  });

  it("tries again on a later run when the dock was too narrow to open it", async () => {
    dock.show.mockReturnValueOnce(false);
    get.mockResolvedValue({ active: false });
    const narrow = render(<BrowserRunPreview agentName="Friday" site={null} />);
    await flush();
    expect(window.sessionStorage.getItem(AUTO_OPENED_KEY)).toBeNull();
    narrow.unmount();
    render(<BrowserRunPreview agentName="Friday" site={null} />);
    await flush();
    expect(dock.show).toHaveBeenCalledTimes(2);
  });

  it("leaves the stream to an open Browser panel: the line alone, no second connection", async () => {
    dock.openPanelIds = ["browser"];
    render(<BrowserRunPreview agentName="Friday" site="example.com" />);
    await flush();
    expect(screen.getByText("Friday is browsing example.com")).toBeInTheDocument();
    expect(screen.queryByTestId("thumbnail")).toBeNull();
    expect(get).not.toHaveBeenCalled();
  });
});
