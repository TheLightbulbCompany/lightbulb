"use client";

import { Button } from "@/components/ui/button";

/** The three pre-typed replies. The backend's messaging.FIRST_REPLIES carries the same strings;
 *  onboarding_brief.first_message_intake recognises the third by its "Not sure yet" prefix. */
export const FIRST_REPLIES = [
  "Here's our website: ",
  "I'm starting ",
  "Not sure yet. Pick something useful and show me how you work.",
] as const;

/** Suggested replies above the composer in a fresh starter channel. A tap PREFILLS the
 *  composer; the person completes the sentence and sends. It never sends on its own. */
export function FirstReplyChips({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="room-composer flex flex-wrap gap-2 pb-0" role="group" aria-label="Suggested replies">
      {FIRST_REPLIES.map((text) => (
        <Button key={text} type="button" variant="outline" size="sm" onClick={() => onPick(text)}>
          {text.trim()}
        </Button>
      ))}
    </div>
  );
}
