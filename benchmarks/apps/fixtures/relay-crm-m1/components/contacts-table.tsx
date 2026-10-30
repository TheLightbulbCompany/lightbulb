"use client";
import Link from "next/link";
import { useState } from "react";

type Row = { id: string; name: string; email: string; companyName: string | null };

/** The search box filters the rows already on the page by name or email as you type. */
export function ContactsTable({ rows }: { rows: Row[] }) {
  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();
  const shown = rows.filter((r) => !needle || r.name.toLowerCase().includes(needle) || r.email.toLowerCase().includes(needle));
  if (rows.length === 0) return <p data-testid="contacts-empty" className="text-slate-500">No contacts yet.</p>;
  return (
    <>
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search by name or email" data-testid="contacts-search" className="w-full rounded border px-3 py-2" />
      <ul data-testid="contacts-list" className="divide-y rounded border bg-white">
        {shown.map((r) => (
          <li key={r.id} data-testid="contact-row" className="flex items-center justify-between px-4 py-3">
            <span>
              <span data-testid="contact-row-name" className="font-medium">{r.name}</span>{" "}
              <span data-testid="contact-row-email" className="text-slate-500">{r.email}</span>
            </span>
            <span className="flex items-center gap-3 text-sm text-slate-500">
              <span data-testid="contact-row-company">{r.companyName ?? ""}</span>
              <Link href={`/contacts/${r.id}`} data-testid="contact-row-link" className="underline">Open</Link>
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}
