"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

const input = "w-full rounded border px-3 py-2";
type Field = { name: string; label: string; testid: string; type?: string };

/** A create form for a shared list: posts to /api/<kind> and goes back to the list. */
export function SimpleForm({ kind, title, fields, submitTestid }: { kind: "practitioners" | "services"; title: string; fields: Field[]; submitTestid: string }) {
  const router = useRouter();
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const r = await fetch(`/api/${kind}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(Object.fromEntries(new FormData(e.currentTarget))) });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    router.push(`/${kind}`);
    router.refresh();
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <h1 className="text-xl font-semibold">{title}</h1>
      {fields.map((f) => <input key={f.name} name={f.name} type={f.type} placeholder={f.label} data-testid={f.testid} className={input} />)}
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      <button type="submit" data-testid={submitTestid} className="rounded bg-slate-900 px-4 py-2 text-white">Save</button>
    </form>
  );
}
