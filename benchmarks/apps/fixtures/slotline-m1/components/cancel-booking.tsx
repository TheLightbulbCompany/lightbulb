"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

export function CancelBooking({ id }: { id: string }) {
  const router = useRouter();
  const [asking, setAsking] = useState(false);
  const btn = "rounded border px-3 py-1.5";
  async function cancel() {
    await fetch(`/api/bookings/${id}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ status: "cancelled" }) });
    setAsking(false);
    router.refresh();
  }
  return asking ? (
    <div className="flex items-center gap-2">
      <span>Cancel this booking?</span>
      <button data-testid="booking-cancel-confirm" className={btn} onClick={cancel}>Yes, cancel it</button>
      <button className={btn} onClick={() => setAsking(false)}>Keep it</button>
    </div>
  ) : (
    <button data-testid="booking-cancel-button" className={btn} onClick={() => setAsking(true)}>Cancel booking</button>
  );
}
