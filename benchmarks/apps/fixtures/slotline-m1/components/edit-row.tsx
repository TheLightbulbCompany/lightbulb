"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

type Field = { key: string; label: string; value: string | number; type?: string };

/** One row of a shared list (practitioners or services) that can be edited in place or deleted. */
export function EditRow({ kind, id, fields }: { kind: "practitioners" | "services"; id: string; fields: Field[] }) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");
  const one = kind.slice(0, -1);
  const api = `/api/${kind}/${id}`;
  async function save(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const r = await fetch(api, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(Object.fromEntries(new FormData(e.currentTarget))) });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    setEditing(false);
    setError("");
    router.refresh();
  }
  async function remove() {
    const r = await fetch(api, { method: "DELETE" });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    router.refresh();
  }
  const btn = "rounded border px-2 py-1 text-sm";
  return (
    <li data-testid={`${one}-row`} className="space-y-2 px-4 py-3">
      {editing ? (
        <form onSubmit={save} className="flex gap-2">
          {fields.map((f) => <input key={f.key} name={f.key} type={f.type} defaultValue={f.value} aria-label={f.label} className="w-full rounded border px-2 py-1" />)}
          <button type="submit" className={btn}>Save</button>
          <button type="button" className={btn} onClick={() => setEditing(false)}>Cancel</button>
        </form>
      ) : (
        <div className="flex items-center justify-between">
          <span><span data-testid={`${one}-row-name`}>{fields[0].value}</span> <span className="text-sm text-slate-500">{fields[1].value}{fields[1].key === "durationMinutes" && " min"}</span></span>
          <span className="flex gap-2">
            <button className={btn} onClick={() => setEditing(true)}>Edit</button>
            <button className={btn} onClick={remove}>Delete</button>
          </span>
        </div>
      )}
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
    </li>
  );
}
