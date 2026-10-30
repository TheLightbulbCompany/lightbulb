"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { dollars } from "@/lib/money";

const input = "w-full rounded border px-3 py-2";
type Item = { id: string; name: string; description: string; priceCents: number };

export function MenuManager({ restaurantId, menu }: { restaurantId: string; menu: Item[] }) {
  const router = useRouter();
  const [error, setError] = useState("");
  const base = `/api/restaurants/${restaurantId}/menu-items`;
  async function add(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const b = Object.fromEntries(new FormData(form)) as Record<string, string>;
    const r = await fetch(base, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ ...b, priceCents: b.priceCents === "" ? null : Number(b.priceCents) }) });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    setError("");
    form.reset();
    router.refresh();
  }
  async function remove(id: string) {
    await fetch(`${base}/${id}`, { method: "DELETE" });
    router.refresh();
  }
  return (
    <div className="space-y-4">
      <ul data-testid="menu-list" className="divide-y rounded border bg-white">
        {menu.map((m) => (
          <li key={m.id} data-testid="menu-item" className="flex items-center justify-between px-4 py-2">
            <span><span data-testid="menu-item-name">{m.name}</span> <span className="text-sm text-slate-500">{m.description}</span></span>
            <span className="flex items-center gap-3">
              <span data-cents={m.priceCents}>{dollars(m.priceCents)}</span>
              <button data-testid="menu-item-delete" className="rounded border px-2 py-1 text-sm" onClick={() => remove(m.id)}>Delete</button>
            </span>
          </li>
        ))}
      </ul>
      <form onSubmit={add} noValidate className="space-y-3">
        <input name="name" placeholder="Item name" data-testid="menu-item-form-name" className={input} />
        <input name="description" placeholder="Description" data-testid="menu-item-form-description" className={input} />
        <input name="priceCents" type="number" step="1" min="0" placeholder="Price in cents" data-testid="menu-item-form-price-cents" className={input} />
        {error && <p role="alert" data-testid="menu-item-form-error" className="text-sm text-red-600">{error}</p>}
        <button type="submit" data-testid="menu-item-form-submit" className="rounded bg-slate-900 px-4 py-2 text-white">Add item</button>
      </form>
    </div>
  );
}
