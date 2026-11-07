"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { TicketForm } from "./ticket-form";

type Ticket = { id: string; subject: string; body: string; priority: string; status: string };

export function TicketActions({ ticket }: { ticket: Ticket }) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const btn = "rounded border px-3 py-1.5";
  async function setStatus(status: string) {
    await fetch(`/api/tickets/${ticket.id}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ status }) });
    router.refresh();
  }
  async function remove() {
    await fetch(`/api/tickets/${ticket.id}`, { method: "DELETE" });
    router.push("/tickets");
    router.refresh();
  }
  if (editing) return <TicketForm initial={ticket} onDone={() => setEditing(false)} />;
  return (
    <div className="flex gap-2">
      <button data-testid="ticket-edit" className={btn} onClick={() => setEditing(true)}>Edit</button>
      {ticket.status === "closed"
        ? <button data-testid="ticket-reopen" className={btn} onClick={() => setStatus("open")}>Reopen</button>
        : <button data-testid="ticket-close" className={btn} onClick={() => setStatus("closed")}>Close</button>}
      <button data-testid="ticket-delete" className={btn} onClick={remove}>Delete</button>
    </div>
  );
}
