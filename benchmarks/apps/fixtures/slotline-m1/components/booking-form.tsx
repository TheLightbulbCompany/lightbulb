"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

const input = "w-full rounded border px-3 py-2";
type Named = { id: string; name: string };

export function BookingForm({ practitioners, services }: { practitioners: Named[]; services: Named[] }) {
  const router = useRouter();
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const b = Object.fromEntries(new FormData(e.currentTarget)) as Record<string, string>;
    // startLocal is clinic wall-clock time; the server turns it into UTC.
    const r = await fetch("/api/bookings", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(b) });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    router.push("/bookings");
    router.refresh();
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <h1 className="text-xl font-semibold">Book an appointment</h1>
      <select name="practitionerId" defaultValue="" data-testid="booking-form-practitioner" className={input}>
        <option value="" disabled>Choose a practitioner</option>
        {practitioners.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select>
      <select name="serviceId" defaultValue="" data-testid="booking-form-service" className={input}>
        <option value="" disabled>Choose a service</option>
        {services.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
      </select>
      <label className="block text-sm text-slate-500">Start (clinic time)
        <input name="startLocal" type="datetime-local" data-testid="booking-form-start" className={input} />
      </label>
      {error && <p role="alert" data-testid="booking-form-error" className="text-sm text-red-600">{error}</p>}
      <button type="submit" data-testid="booking-form-submit" className="rounded bg-slate-900 px-4 py-2 text-white">Book</button>
    </form>
  );
}
