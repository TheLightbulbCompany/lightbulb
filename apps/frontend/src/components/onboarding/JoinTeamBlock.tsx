"use client";

import { useId, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { joinLinkTarget } from "@/lib/workspaceJoin";

/** For someone joining a team, not starting one: the invitation email, or a pasted join link.
 *  The join page handles sign-in/sign-up from there. `compact` is the quiet line under the
 *  sign-up form; it takes its look from the page's `.auth-join` class. */
export function JoinTeamBlock({ compact = false }: { compact?: boolean }) {
  const router = useRouter();
  const id = useId();
  const [link, setLink] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  function join(event: FormEvent) {
    event.preventDefault();
    const target = joinLinkTarget(link);
    if (!target) { setProblem("That doesn't look like a Lightbulb join link."); return; }
    router.push(target);
  }
  const form = (
    <form onSubmit={join} className="flex w-full flex-col gap-1.5">
      <label htmlFor={`${id}-link`} className={compact ? undefined : "text-sm font-medium"}>
        {compact ? "Have a join link?" : "Paste a join link"}
      </label>
      <div className="flex gap-2">
        <Input id={`${id}-link`} value={link} onChange={(e) => { setLink(e.target.value); setProblem(null); }} placeholder="https://…/join/…"
          spellCheck={false} autoCapitalize="none" autoComplete="off" aria-describedby={problem ? `${id}-problem` : undefined} className="min-w-0 flex-1" />
        <Button type="submit" variant="outline" disabled={!link.trim()}>Join</Button>
      </div>
      {problem && <p id={`${id}-problem`} role="alert" className="text-sm text-destructive">{problem}</p>}
    </form>
  );
  if (compact) {
    return (
      <div className="auth-join">
        <p>Joining a team? If you got an invitation email, open it on this device.</p>
        {form}
      </div>
    );
  }
  return (
    <section aria-labelledby={`${id}-heading`} className="flex w-full max-w-md flex-col gap-3 p-2 text-muted-foreground">
      <h2 id={`${id}-heading`} className="text-base font-medium text-foreground">Joining a team instead?</h2>
      <p className="text-sm">Invited by email? Open that email on this device and tap its link.</p>
      {form}
    </section>
  );
}
