"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { dollars } from "@/lib/money";

type Item = { id: string; name: string; description: string; priceCents: number };

/** The menu with a cart. The cart total shown here is only a preview: the server prices the order itself. */
export function OrderCart({ restaurantId, menu }: { restaurantId: string; menu: Item[] }) {
  const router = useRouter();
  const [qty, setQty] = useState<Record<string, string>>({});
  const [cart, setCart] = useState<Record<string, number>>({});
  const [error, setError] = useState("");
  const subtotal = menu.reduce((s, m) => s + m.priceCents * (cart[m.id] ?? 0), 0);
  const add = (id: string) => {
    const q = Math.floor(Number(qty[id] ?? 1));
    if (q >= 1) setCart({ ...cart, [id]: (cart[id] ?? 0) + q });
  };
  async function place() {
    const items = Object.entries(cart).map(([menuItemId, quantity]) => ({ menuItemId, quantity }));
    const r = await fetch("/api/orders", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ restaurantId, items }) });
    const b = await r.json().catch(() => null);
    if (!r.ok) return setError(b?.error ?? "Something went wrong");
    router.push(`/orders/${b.id}`);
  }
  return (
    <div className="space-y-4">
      <ul data-testid="menu-list" className="divide-y rounded border bg-white">
        {menu.map((m) => (
          <li key={m.id} data-testid="menu-item" className="flex items-center justify-between px-4 py-2">
            <span><span data-testid="menu-item-name">{m.name}</span> <span className="text-sm text-slate-500">{m.description}</span></span>
            <span className="flex items-center gap-3">
              <span data-cents={m.priceCents}>{dollars(m.priceCents)}</span>
              <input data-testid="menu-item-qty" type="number" min="1" max="99" value={qty[m.id] ?? "1"} onChange={(e) => setQty({ ...qty, [m.id]: e.target.value })} className="w-16 rounded border px-2 py-1" />
              <button data-testid="menu-item-add" className="rounded border px-2 py-1 text-sm" onClick={() => add(m.id)}>Add</button>
            </span>
          </li>
        ))}
      </ul>
      <p>Cart: <span data-testid="cart-subtotal" data-cents={subtotal}>{dollars(subtotal)}</span></p>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      <button data-testid="place-order-button" disabled={Object.keys(cart).length === 0} className="rounded bg-slate-900 px-4 py-2 text-white disabled:opacity-40" onClick={place}>Place order</button>
    </div>
  );
}
