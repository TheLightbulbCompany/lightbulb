import { redirect } from "next/navigation";
import { resolvePanelAlias, settingsHrefFor } from "@/components/workspace/panelRegistry";

// /chat is the Lightbulb place now (founder, 8 Nov 2026): the installed Mac app and old links open /chat, so they land
// on /voice/lightbulb with their query string. The super-agent WorkspaceShell stays in the repo, unrouted.
export default async function ChatPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(await searchParams)) for (const v of [value].flat()) if (v !== undefined) qs.append(key, v);
  const provider = qs.get("provider");
  const providerSetup = provider === "chatgpt_oauth" || provider === "openrouter_managed";
  if (provider === "chatgpt_oauth" && !qs.has("action")) qs.set("action", "connect-chatgpt");
  const settingsIntent = resolvePanelAlias(qs.get("panel") ?? "") === "settings" || settingsHrefFor(qs.toString()) !== null;
  const destination = providerSetup || settingsIntent ? "/voice/settings" : "/voice/lightbulb";
  const q = qs.toString();
  redirect(q ? `${destination}?${q}` : destination);
}
