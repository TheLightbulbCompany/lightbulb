import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { EngineFault } from "../EngineFault";

const directory = vi.hoisted(() => ({ down: false }));
vi.mock("@/components/messaging/engineDirectory", () => ({ useEngineDirectory: () => ({ records: [], down: directory.down }) }));
const invoke = vi.fn(() => Promise.resolve());
afterEach(() => { invoke.mockClear(); directory.down = false; delete (window as unknown as { __TAURI__?: unknown }).__TAURI__; });

it("shows nothing while the service is up", () => {
  const { container } = render(<EngineFault />);
  expect(container).toBeEmptyDOMElement();
});

it("names the fault and Fix runs engine_fix", () => {
  directory.down = true;
  (window as unknown as { __TAURI__: unknown }).__TAURI__ = { core: { invoke } };
  render(<EngineFault />);
  expect(screen.getByText("Lightbulb's background service is off")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Fix" }));
  expect(invoke).toHaveBeenCalledWith("engine_fix");
});
