"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

const input = "w-full rounded border px-3 py-2";

export function SettingsForm({ orgId, name, description }: { orgId: string; name: string; description: string }) {
  const router = useRouter();
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setSaved(false);
    const r = await fetch(`/api/orgs/${orgId}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(Object.fromEntries(new FormData(e.currentTarget))) });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    setError("");
    setSaved(true);
    router.refresh();
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <input name="name" defaultValue={name} placeholder="Name" data-testid="settings-name-input" className={input} />
      <textarea name="description" defaultValue={description} placeholder="Description" data-testid="settings-description-input" className={input} />
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      {saved && <p data-testid="settings-saved" className="text-sm text-green-700">Saved</p>}
      <button type="submit" data-testid="settings-save" className="rounded bg-slate-900 px-4 py-2 text-white">Save</button>
    </form>
  );
}
