// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const get = vi.fn();
const dock = vi.hoisted(() => ({ show: vi.fn(() => true) }));
const apiClient = { get };
vi.mock("@/lib/api", () => ({ useApi: () => apiClient }));
vi.mock("@/components/workspace/workspaceController", () => ({ showBrowserBesideChat: dock.show }));

import { browsingSite } from "@lightbulb/shared";
import { BrowserRunPreview } from "../BrowserRunPreview";
import { _resetSharedLiveViewForTests, useSharedLiveView } from "@/components/workspace/panels/sharedLiveView";

const tool = (name: string, args: unknown = {}) => ({ id: name, name, args, result: null, status: "done" as const });

async function flush(): Promise<void> {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

beforeEach(() => {
  get.mockReset();
  dock.show.mockClear();
  _resetSharedLiveViewForTests();
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

function Watchers(): React.ReactElement {
  const { watchers, source } = useSharedLiveView();
  return <output data-testid="watchers" data-count={watchers} data-url={source?.url ?? ""} />;
}

describe("BrowserRunPreview", () => {
  it("shows a live preview by default, never opens the panel itself, and opens it on click", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1" });
    render(<><BrowserRunPreview agentName="Friday" site="supademo.com" /><Watchers /></>);
    await flush();
    expect(screen.getByText("Friday is browsing supademo.com")).toBeInTheDocument();
    // A copy of the page's one connection, not a connection of its own.
    expect(screen.getByTestId("browser-mirror")).toBeInTheDocument();
    expect(screen.getByTestId("watchers")).toHaveAttribute("data-count", "1");
    expect(screen.getByTestId("watchers")).toHaveAttribute("data-url", "https://signed");
    // The live view fills its nearest positioned ancestor: without this the preview covered
    // the whole run card and clicks landed on its hidden Stop/byline.
    expect(screen.getByRole("button", { name: /open the shared browser/i })).toHaveClass("relative");
    expect(dock.show).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /open the shared browser/i }));
    expect(dock.show).toHaveBeenLastCalledWith({ focus: true, asTabWhenNarrow: true });
  });

  it("watches nothing while no browser session is live", async () => {
    get.mockResolvedValue({ active: false });
    render(<><BrowserRunPreview agentName="Friday" site={null} /><Watchers /></>);
    await flush();
    expect(screen.queryByTestId("browser-mirror")).toBeNull();
    expect(screen.getByTestId("watchers")).toHaveAttribute("data-count", "0");
  });
});
