// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const api = vi.hoisted(() => ({ get: () => Promise.resolve({ active: true, url: "https://signed" }) }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
const controller = vi.hoisted(() => ({ showBrowserBesideChat: vi.fn(), showMacRunInBrowser: vi.fn(() => true) }));
vi.mock("@/components/workspace/workspaceController", () => controller);
vi.mock("@/components/workspace/panels/sharedLiveView", () => ({
  useSharedLiveView: () => ({ status: "elsewhere" }),
  useLiveViewWatch: () => undefined,
  LiveViewMirror: () => <div data-testid="browser-mirror" />,
}));
vi.mock("@/components/workspace/panels/BrowserFrameView", () => ({
  BrowserFrameView: ({ runId, interactive, heartbeat }: { runId?: string; interactive?: boolean; heartbeat?: string }) =>
    <div data-testid="frames" data-run={runId} data-interactive={String(!!interactive)} data-heartbeat={heartbeat} />,
}));

import { BrowserRunPreview } from "../BrowserRunPreview";

it("shows the run's own tab as live frames when another screen holds the live view", async () => {
  render(<BrowserRunPreview agentName="Friday" site={null} runId="run-1" />);
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  expect(screen.getByTestId("frames")).toHaveAttribute("data-run", "run-1");
  // A picture: a click on the card opens the panel, where the person sees what they click.
  expect(screen.getByTestId("frames")).toHaveAttribute("data-interactive", "false");
  expect(screen.queryByTestId("browser-mirror")).toBeNull();
});

it("a run driving a Mac tab shows that tab's frames, view-only, beating the Mac heartbeat", async () => {
  const { MacBrowserPreview, usesMacBrowser } = await import("../BrowserRunPreview");
  expect(usesMacBrowser({ tools: [{ name: "lightbulb_desktop", args: { action: "shell" } }] })).toBe(false);
  expect(usesMacBrowser({ tools: [{ name: "lightbulb_desktop", args: { action: "browser" } }] })).toBe(true);
  render(<MacBrowserPreview agentName="Friday" runId="run-9" />);
  expect(screen.getByText("Friday is browsing on a Mac")).toBeInTheDocument();
  expect(screen.getAllByTestId("frames").at(-1)).toHaveAttribute("data-run", "run-9");
  expect(screen.getAllByTestId("frames").at(-1)).toHaveAttribute("data-interactive", "false");
  expect(screen.getAllByTestId("frames").at(-1)).toHaveAttribute("data-heartbeat", "/browser/frames");
});

it("clicking the Mac preview (label or picture) shows that run in the Browser panel", async () => {
  const { MacBrowserPreview } = await import("../BrowserRunPreview");
  render(<MacBrowserPreview agentName="Forge" runId="run-7" />);
  fireEvent.click(screen.getByRole("button", { name: "Forge is browsing on a Mac. Show it in the Browser panel" }));
  fireEvent.click(screen.getByRole("button", { name: "Forge is browsing on a Mac" }));
  expect(controller.showMacRunInBrowser).toHaveBeenCalledTimes(2);
  expect(controller.showMacRunInBrowser).toHaveBeenCalledWith("run-7");
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("with no dock (the mobile shell) the click shows the same frames full screen, view-only", async () => {
  controller.showMacRunInBrowser.mockReturnValueOnce(false);
  const { MacBrowserPreview } = await import("../BrowserRunPreview");
  render(<MacBrowserPreview agentName="Forge" runId="run-7" />);
  fireEvent.click(screen.getByRole("button", { name: "Forge is browsing on a Mac" }));
  const large = (await screen.findByRole("dialog")).querySelector('[data-testid="frames"]');
  expect(large).toHaveAttribute("data-run", "run-7");
  expect(large).toHaveAttribute("data-interactive", "false");
});

it("reads the Mac a run browses on from its last browser call (legacy 'browser' action)", async () => {
  const { macBrowser } = await import("../BrowserRunPreview");
  expect(macBrowser({ tools: [
    { name: "lightbulb_desktop", args: { action: "browser", deviceId: "a" } },
    { name: "lightbulb_desktop", args: { action: "shell", deviceId: "x" } },
    { name: "lightbulb_desktop", args: { action: "browser", deviceId: "b" } },
  ] })).toEqual({ deviceId: "b" });
  expect(macBrowser({ tools: [] })).toBeUndefined();
});

const doneResult = (obj: unknown) => [{ type: "text", text: JSON.stringify(obj) }];
const useBrowserCall = (status: "running" | "done" | "error", result?: unknown) =>
  ({ name: "lightbulb_desktop", args: { action: "use_browser" }, status, result: result === undefined ? null : doneResult(result) });

describe("usesMacBrowser / macBrowser (use_browser)", () => {
  it("treats a run whose last selection is a successful use_browser as a Mac run", async () => {
    const { usesMacBrowser } = await import("../BrowserRunPreview");
    const run = { tools: [useBrowserCall("done", { browser: "mac", deviceId: "d1", deviceName: "Prasiddha's Mac" })] };
    expect(usesMacBrowser(run)).toBe(true);
  });

  it("a failed or running use_browser is not a Mac run", async () => {
    const { usesMacBrowser, macBrowser } = await import("../BrowserRunPreview");
    const running = { tools: [useBrowserCall("running")] };
    const failed = { tools: [useBrowserCall("done", { error: "No Mac is paired." })] };
    expect(usesMacBrowser(running)).toBe(false);
    expect(usesMacBrowser(failed)).toBe(false);
    expect(macBrowser(running)).toBeUndefined();
    expect(macBrowser(failed)).toBeUndefined();
  });

  it("a run that switched back with use_cloud_browser is not a Mac run", async () => {
    const { usesMacBrowser, macBrowser } = await import("../BrowserRunPreview");
    const run = { tools: [
      useBrowserCall("done", { browser: "mac", deviceId: "d1" }),
      { name: "lightbulb_desktop", args: { action: "use_cloud_browser" }, status: "done", result: doneResult({ browser: "cloud" }) },
    ] };
    expect(usesMacBrowser(run)).toBe(false);
    expect(macBrowser(run)).toBeUndefined();
  });

  it("reads deviceName from the use_browser result", async () => {
    const { macBrowser } = await import("../BrowserRunPreview");
    const run = { tools: [useBrowserCall("done", { browser: "mac", deviceId: "d1", deviceName: "Prasiddha's Mac" })] };
    expect(macBrowser(run)).toEqual({ deviceId: "d1", deviceName: "Prasiddha's Mac" });
    const unnamed = { tools: [useBrowserCall("done", { browser: "mac", deviceId: "d2", deviceName: null })] };
    expect(macBrowser(unnamed)).toEqual({ deviceId: "d2", deviceName: undefined });
  });
});

it("preview label names the Mac", async () => {
  const { MacBrowserPreview } = await import("../BrowserRunPreview");
  render(<MacBrowserPreview agentName="Forge" runId="run-7" deviceName="Prasiddha's Mac" />);
  expect(screen.getByText("Forge is browsing on Prasiddha's Mac")).toBeInTheDocument();
});
