"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

const input = "w-full rounded border px-3 py-2";
type Initial = { id: string; name: string; email: string; phone: string; title: string; companyId: string | null };

/** Create (no `initial`) or edit a contact. */
export function ContactForm({ companies, initial }: { companies: { id: string; name: string }[]; initial?: Initial }) {
  const router = useRouter();
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const r = await fetch(initial ? `/api/contacts/${initial.id}` : "/api/contacts", { method: initial ? "PATCH" : "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(Object.fromEntries(new FormData(e.currentTarget))) });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    if (initial) return router.refresh();
    router.push("/contacts");
    router.refresh();
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <h2 className="text-lg font-semibold">{initial ? "Edit" : "New contact"}</h2>
      <input name="name" defaultValue={initial?.name} placeholder="Name" data-testid="contact-form-name" className={input} />
      <input name="email" defaultValue={initial?.email} type="email" placeholder="Email" data-testid="contact-form-email" className={input} />
      <input name="phone" defaultValue={initial?.phone} placeholder="Phone" className={input} />
      <input name="title" defaultValue={initial?.title} placeholder="Job title" className={input} />
      <select name="companyId" defaultValue={initial?.companyId ?? ""} className={input}>
        <option value="">No company</option>
        {companies.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
      </select>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      <button type="submit" data-testid="contact-form-submit" className="rounded bg-slate-900 px-4 py-2 text-white">Save</button>
    </form>
  );
}
