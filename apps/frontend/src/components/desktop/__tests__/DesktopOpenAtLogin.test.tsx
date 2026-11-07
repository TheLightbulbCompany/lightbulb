import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DesktopOpenAtLogin } from "../DesktopOpenAtLogin";

afterEach(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  delete (window as any).__TAURI__;
});

it("renders nothing outside the desktop app", () => {
  const { container } = render(<DesktopOpenAtLogin />);
  expect(container.innerHTML).toBe("");
});

it("shows the real state and toggles it through the autostart plugin", async () => {
  const invoke = vi.fn(async (cmd: string) => (cmd === "plugin:autostart|is_enabled" ? true : undefined));
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).__TAURI__ = { core: { invoke } };
  render(<DesktopOpenAtLogin />);
  const box = await screen.findByRole("switch", { name: "Open at login" });
  expect(box).toBeChecked();
  await act(async () => { fireEvent.click(box); });
  expect(invoke).toHaveBeenCalledWith("plugin:autostart|disable");
  expect(box).not.toBeChecked();
});
