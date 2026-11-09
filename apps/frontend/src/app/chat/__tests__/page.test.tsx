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

it.each([
  "overview", "usage", "skills", "integrations", "connections", "llm", "credits",
  "teams-settings", "teams-members", "teams-accounts",
  "teams-money", "teams-payments", "teams-products", "teams-customers", "teams-spend",
])("opens the accepted Settings alias %s while preserving all query intent", async (panel) => {
  await ChatPage({ searchParams: Promise.resolve({ panel, action: "change-provider", checkout: "success", tag: ["a", "b"] }) });
  expect(nav.redirect).toHaveBeenLastCalledWith(`/voice/settings?panel=${panel}&action=change-provider&checkout=success&tag=a&tag=b`);
});

it("keeps explicit provider and action intent on an old model settings link", async () => {
  await ChatPage({ searchParams: Promise.resolve({ panel: "llm", provider: "chatgpt_oauth", action: "change-provider", checkout: "success" }) });
  expect(nav.redirect).toHaveBeenLastCalledWith("/voice/settings?panel=llm&provider=chatgpt_oauth&action=change-provider&checkout=success");
});

it.each(["activity", "teams-agents", "constructor", "unknown"])("keeps non-Settings panel %s at Lightbulb", async (panel) => {
  await ChatPage({ searchParams: Promise.resolve({ panel }) });
  expect(nav.redirect).toHaveBeenLastCalledWith(`/voice/lightbulb?panel=${panel}`);
});
