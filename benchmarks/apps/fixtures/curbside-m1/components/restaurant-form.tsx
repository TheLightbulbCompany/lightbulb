"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

const input = "w-full rounded border px-3 py-2";

export function RestaurantForm() {
  const router = useRouter();
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const b = Object.fromEntries(new FormData(e.currentTarget));
    const r = await fetch("/api/restaurants", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(b) });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    router.push("/restaurants");
    router.refresh();
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <h1 className="text-xl font-semibold">Add a restaurant</h1>
      <input name="name" placeholder="Name" data-testid="restaurant-form-name" className={input} />
      <input name="cuisine" placeholder="Cuisine" data-testid="restaurant-form-cuisine" className={input} />
      <input name="address" placeholder="Address" data-testid="restaurant-form-address" className={input} />
      {error && <p role="alert" data-testid="restaurant-form-error" className="text-sm text-red-600">{error}</p>}
      <button type="submit" data-testid="restaurant-form-submit" className="rounded bg-slate-900 px-4 py-2 text-white">Save restaurant</button>
    </form>
  );
}
