"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

const input = "w-full rounded border px-3 py-2";

/** Create (no `initial`) or edit a company. */
export function CompanyForm({ initial }: { initial?: { id: string; name: string; domain: string } }) {
  const router = useRouter();
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const r = await fetch(initial ? `/api/companies/${initial.id}` : "/api/companies", { method: initial ? "PATCH" : "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(Object.fromEntries(new FormData(form))) });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    setError("");
    if (!initial) form.reset();
    router.refresh();
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <h2 className="text-lg font-semibold">{initial ? "Edit" : "New company"}</h2>
      <input name="name" defaultValue={initial?.name} placeholder="Name" data-testid="company-form-name" className={input} />
      <input name="domain" defaultValue={initial?.domain} placeholder="Domain" data-testid="company-form-domain" className={input} />
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      <button type="submit" data-testid="company-form-submit" className="rounded bg-slate-900 px-4 py-2 text-white">Save</button>
    </form>
  );
}
