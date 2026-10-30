import { afterEach, expect, it, vi } from "vitest";
import { hasEngineAsk } from "../localTerminal";

const install = (invoke?: () => Promise<unknown>) => {
  (window as unknown as { __TAURI__?: unknown }).__TAURI__ = invoke && { core: { invoke } };
};
afterEach(() => install());

it("is false in a browser", async () => {
  await expect(hasEngineAsk()).resolves.toBe(false);
});

it("is false on an older app, without the command", async () => {
  install(() => Promise.reject("Command engine_ask not found"));
  await expect(hasEngineAsk()).resolves.toBe(false);
});

it("is true when the app refuses the empty op, and never asks for a real one", async () => {
  const invoke = vi.fn(() => Promise.reject("engine op not allowed: "));
  install(invoke);
  await expect(hasEngineAsk()).resolves.toBe(true);
  expect(invoke).toHaveBeenCalledWith("engine_ask", { op: "" });
});
