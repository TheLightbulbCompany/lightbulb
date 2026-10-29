import { Terminal } from "lucide-react";
import { sessionToolName } from "@lightbulb/shared";
import { ClaudeIcon, OpenAIIcon } from "@/components/chat/ProviderIcons";
import { cn } from "@/lib/utils";

/** The coding tool a shared session comes from, as its logo. Codex wears the
 *  OpenAI mark in the ground's ink; an unknown tool gets a neutral terminal.
 *  Cursor (its cube) and Grok CLI (xAI's slashed X) are drawn in ink too. */
export function SessionToolIcon({ provider, className }: { provider: string; className?: string }) {
  const name = sessionToolName(provider);
  const props = { role: "img", "aria-label": name, className: cn("size-4 shrink-0", className) };
  if (provider === "claude") return <ClaudeIcon {...props} />;
  if (provider === "codex") return <OpenAIIcon fill="currentColor" {...props} />;
  if (provider === "cursor") {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinejoin="round" {...props}>
        <path d="M12 2.5 20.5 7.25v9.5L12 21.5 3.5 16.75v-9.5Z" />
        <path d="M3.5 7.25 12 12l8.5-4.75M12 12v9.5" />
      </svg>
    );
  }
  if (provider === "grok") {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" {...props}>
        <path d="M5 4.5 15.5 19.5M19 4.5 5 19.5" />
      </svg>
    );
  }
  return <Terminal {...props} />;
}
