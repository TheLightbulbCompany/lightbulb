import { expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ redirect: vi.fn() }));
vi.mock("next/navigation", () => nav);

import ChatPage from "../page";

it("/chat lands on /voice/lightbulb", async () => {
  await ChatPage({ searchParams: Promise.resolve({}) });
  expect(nav.redirect).toHaveBeenCalledWith("/voice/lightbulb");
});

it("keeps the query string, repeated keys included", async () => {
  await ChatPage({ searchParams: Promise.resolve({ panel: "chat", room: ["a", "b"], x: undefined }) });
  expect(nav.redirect).toHaveBeenCalledWith("/voice/lightbulb?panel=chat&room=a&room=b");
});

it("returns provider checkout to the reachable settings controls, preserving intent", async () => {
  await ChatPage({ searchParams: Promise.resolve({ provider: "chatgpt_oauth", checkout: "success" }) });
  expect(nav.redirect).toHaveBeenCalledWith("/voice/settings?provider=chatgpt_oauth&checkout=success&action=connect-chatgpt");
  await ChatPage({ searchParams: Promise.resolve({ provider: "openrouter_managed", checkout: "success" }) });
  expect(nav.redirect).toHaveBeenCalledWith("/voice/settings?provider=openrouter_managed&checkout=success");
});

it("opens old shell settings links in the current Settings place", async () => {
  await ChatPage({ searchParams: Promise.resolve({ panel: "settings", section: "billing" }) });
  expect(nav.redirect).toHaveBeenCalledWith("/voice/settings?panel=settings&section=billing");
});
