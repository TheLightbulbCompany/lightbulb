import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DeployRefresh, isChunkLoadError, reloadBlocked } from "@/components/DeployRefresh";

const reload = vi.fn();

beforeEach(() => {
  reload.mockReset();
  sessionStorage.clear();
  vi.stubGlobal("location", { ...window.location, reload });
  vi.stubEnv("NEXT_PUBLIC_BUILD_ID", "old");
});

afterEach(() => {
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("reloadBlocked", () => {
  it("is safe on a quiet page, including an empty composer", () => {
    document.body.innerHTML = "<textarea></textarea><p>hello</p>";
    expect(reloadBlocked()).toBe(false);
  });

  it.each([
    ["unsent composer text", "<textarea>half a sentence</textarea>"],
    ["an open dialog", "<div role='dialog'></div>"],
    ["a live run or the browser under control", "<article data-reload-blocker></article>"],
  ])("blocks on %s", (_, html) => {
    document.body.innerHTML = html;
    expect(reloadBlocked()).toBe(true);
  });
});

describe("DeployRefresh", () => {
  const newDeploy = () => vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ json: async () => ({ buildId: "new" }) }));

  it("reloads on a new deploy when nothing blocks", async () => {
    newDeploy();
    render(<DeployRefresh />);
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("shows the pill instead while the user is typing", async () => {
    newDeploy();
    document.body.innerHTML = "<textarea>unsent</textarea>";
    render(<DeployRefresh />);
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    expect(reload).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("Lightbulb updated");
  });

  it("stays put on the same build", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ json: async () => ({ buildId: "old" }) }));
    render(<DeployRefresh />);
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    expect(reload).not.toHaveBeenCalled();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("reloads immediately on a ChunkLoadError, once", () => {
    render(<DeployRefresh />);
    const chunkError = Object.assign(new Error("Loading chunk 123 failed."), { name: "ChunkLoadError" });
    const rejection = () => window.dispatchEvent(Object.assign(new Event("unhandledrejection"), { reason: chunkError }));
    rejection();
    rejection();
    expect(reload).toHaveBeenCalledTimes(1);
  });
});

it("recognises chunk failures and nothing else", () => {
  expect(isChunkLoadError(new TypeError("Failed to fetch dynamically imported module: /x.js"))).toBe(true);
  expect(isChunkLoadError(new Error("boom"))).toBe(false);
});
