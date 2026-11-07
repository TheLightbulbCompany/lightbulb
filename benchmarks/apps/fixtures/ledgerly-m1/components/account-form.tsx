"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

const input = "w-full rounded border px-3 py-2";

export function AccountForm() {
  const router = useRouter();
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const r = await fetch("/api/accounts", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(Object.fromEntries(new FormData(e.currentTarget))) });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    router.push("/accounts");
    router.refresh();
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <h1 className="text-xl font-semibold">New account</h1>
      <input name="code" placeholder="Code" data-testid="account-form-code" className={input} />
      <input name="name" placeholder="Name" data-testid="account-form-name" className={input} />
      <select name="type" defaultValue="debit" data-testid="account-form-type" className={input}>
        <option value="debit">debit</option>
        <option value="credit">credit</option>
      </select>
      {error && <p role="alert" data-testid="account-form-error" className="text-sm text-red-600">{error}</p>}
      <button type="submit" data-testid="account-form-submit" className="rounded bg-slate-900 px-4 py-2 text-white">Save</button>
    </form>
  );
}
