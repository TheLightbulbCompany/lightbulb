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
