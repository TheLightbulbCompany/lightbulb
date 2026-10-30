"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

const input = "w-full rounded border px-3 py-2";

export function CreateOrgForm() {
  const router = useRouter();
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const r = await fetch("/api/orgs", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(Object.fromEntries(new FormData(e.currentTarget))) });
    const body = await r.json().catch(() => null);
    if (!r.ok) return setError(body?.error ?? "Something went wrong");
    router.push(`/orgs/${body.id}`);
    router.refresh();
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <h1 className="text-xl font-semibold">New organization</h1>
      <input name="name" placeholder="Name" data-testid="org-name-input" className={input} />
      <input name="slug" placeholder="slug-in-lowercase" data-testid="org-slug-input" className={input} />
      {error && <p role="alert" data-testid="create-org-error" className="text-sm text-red-600">{error}</p>}
      <button type="submit" data-testid="create-org-submit" className="rounded bg-slate-900 px-4 py-2 text-white">Create</button>
    </form>
  );
}
