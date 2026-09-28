// @vitest-environment jsdom
import { expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";

const api = vi.hoisted(() => ({ get: () => Promise.resolve({ active: true, url: "https://signed" }) }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
vi.mock("@/components/workspace/workspaceController", () => ({ showBrowserBesideChat: vi.fn() }));
vi.mock("@/components/workspace/panels/sharedLiveView", () => ({
  useSharedLiveView: () => ({ status: "elsewhere" }),
  useLiveViewWatch: () => undefined,
  LiveViewMirror: () => <div data-testid="browser-mirror" />,
}));
vi.mock("@/components/workspace/panels/BrowserFrameView", () => ({
  BrowserFrameView: ({ runId, interactive }: { runId?: string; interactive?: boolean }) =>
    <div data-testid="frames" data-run={runId} data-interactive={String(!!interactive)} />,
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
