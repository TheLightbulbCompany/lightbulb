import { redirect } from "next/navigation";

// Old `?panel=` keys that now live on the Settings page: old links, mobile "Manage on web" links and the backend's
// action_href still carry them.
const SETTINGS_PANELS = new Set([
  "settings", "overview", "usage", "skills", "integrations", "connections", "llm", "credits",
  "teams-settings", "teams-members", "teams-accounts",
  "teams-money", "teams-payments", "teams-products", "teams-customers", "teams-spend",
]);

// /chat is the Lightbulb place now (founder, 8 Nov 2026): the installed Mac app and old links open /chat, so they land
// on /voice/lightbulb with their query string.
export default async function ChatPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(await searchParams)) for (const v of [value].flat()) if (v !== undefined) qs.append(key, v);
  const provider = qs.get("provider");
  const providerSetup = provider === "chatgpt_oauth" || provider === "openrouter_managed";
  if (provider === "chatgpt_oauth" && !qs.has("action")) qs.set("action", "connect-chatgpt");
  const destination = providerSetup || SETTINGS_PANELS.has(qs.get("panel") ?? "") ? "/voice/settings" : "/voice/lightbulb";
  const q = qs.toString();
  redirect(q ? `${destination}?${q}` : destination);
}
