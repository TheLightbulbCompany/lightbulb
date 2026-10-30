"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

const input = "w-full rounded border px-3 py-2";
type Initial = { id: string; subject: string; body: string; priority: string };

/** Create a ticket, or edit `initial` when given. */
export function TicketForm({ initial, onDone }: { initial?: Initial; onDone?: () => void }) {
  const router = useRouter();
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const b = Object.fromEntries(new FormData(e.currentTarget)) as Record<string, string>;
    if (!b.subject.trim()) return setError("Subject is required");
    const r = await fetch(initial ? `/api/tickets/${initial.id}` : "/api/tickets", { method: initial ? "PATCH" : "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(b) });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    if (onDone) onDone();
    else router.push("/tickets");
    router.refresh();
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <input name="subject" placeholder="Subject" defaultValue={initial?.subject} data-testid="ticket-subject" className={input} />
      <textarea name="body" placeholder="Describe the problem" defaultValue={initial?.body} data-testid="ticket-body" className={input} />
      <select name="priority" defaultValue={initial?.priority ?? "medium"} data-testid="ticket-priority" className={input}>
        <option value="low">low</option>
        <option value="medium">medium</option>
        <option value="high">high</option>
      </select>
      {error && <p role="alert" data-testid="ticket-error" className="text-sm text-red-600">{error}</p>}
      <button type="submit" data-testid="ticket-submit" className="rounded bg-slate-900 px-4 py-2 text-white">{initial ? "Save changes" : "Create ticket"}</button>
    </form>
  );
}
