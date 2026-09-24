import { Terminal } from "lucide-react";
import { sessionToolName } from "@lightbulb/shared";
import { ClaudeIcon, OpenAIIcon } from "@/components/chat/ProviderIcons";
import { cn } from "@/lib/utils";

/** The coding tool a shared session comes from, as its logo. Codex wears the
 *  OpenAI mark in the ground's ink; an unknown tool gets a neutral terminal. */
export function SessionToolIcon({ provider, className }: { provider: string; className?: string }) {
  const name = sessionToolName(provider);
  const props = { role: "img", "aria-label": name, className: cn("size-4 shrink-0", className) };
  if (provider === "claude") return <ClaudeIcon {...props} />;
  if (provider === "codex") return <OpenAIIcon fill="currentColor" {...props} />;
  return <Terminal {...props} />;
}
