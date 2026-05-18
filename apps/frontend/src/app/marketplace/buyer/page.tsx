"use client";
import useSWR from "swr";
import { useApi } from "@/lib/api";

interface Purchase {
  purchase_id: string;
  listing_id: string;
  listing_slug?: string;
  license_key: string;
  price_paid_cents: number;
  status: "paid" | "refunded" | "revoked";
  created_at: string;
}

interface PurchasesResponse {
  items?: Purchase[];
}

export default function Buyer() {
  const api = useApi();
  const { data, error } = useSWR<PurchasesResponse>(
    "/marketplace/my-purchases",
    (path: string) => api.get(path) as Promise<PurchasesResponse>,
  );
  if (error) return <p className="p-8 text-[var(--danger)]">Couldn&apos;t load purchases: {error.message}</p>;
  if (!data) return <p className="p-8">Loading…</p>;
  const items: Purchase[] = data?.items ?? [];
  return (
    <main className="max-w-3xl mx-auto px-6 py-12">
      <h1 className="font-editorial-serif text-4xl mb-8">Your purchases</h1>
      {items.length === 0 ? (
        <p className="text-[var(--mute)]">No purchases yet.</p>
      ) : (
        <div className="space-y-3">
          {items.map((p) => (
            <div key={p.purchase_id} className="editorial-card p-4">
              <div className="flex justify-between">
                <span className="font-semibold">{p.listing_slug ?? p.listing_id}</span>
                <span className="text-[var(--mute)] text-sm">
                  ${(p.price_paid_cents / 100).toFixed(2)}
                </span>
              </div>
              <p className="text-xs text-[var(--mute-soft)] mt-2">
                Status: {p.status} · License: <code>{p.license_key}</code>
              </p>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
