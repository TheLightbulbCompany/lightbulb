"use client";

import { Button } from "@/components/ui/button";

/** The three pre-typed replies;
 *  onboarding_brief.first_message_intake recognises the third by its "Not sure yet" prefix. */
export const FIRST_REPLIES = [
  "Here's our website: ",
  "I'm starting ",
  "Not sure yet. Pick something useful and show me how you work.",
] as const;

/** The two prefixes that need completing; the composer treats them as empty so they cannot be sent. */
export const INCOMPLETE_FIRST_REPLIES: readonly string[] = FIRST_REPLIES.slice(0, 2).map((t) => t.trim());

/** Suggested replies above the composer in a fresh starter channel. A tap PREFILLS the
 *  composer; the person completes the sentence and sends. It never sends on its own. */
export function FirstReplyChips({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="mx-5 mb-2 flex flex-wrap gap-2" role="group" aria-label="Suggested replies">
      {FIRST_REPLIES.map((text) => (
        <Button key={text} type="button" variant="outline" size="sm" onClick={() => onPick(text)}>
          {text.trim()}
        </Button>
      ))}
    </div>
  );
}
