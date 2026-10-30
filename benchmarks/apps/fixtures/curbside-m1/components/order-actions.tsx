"use client";
import { useRouter } from "next/navigation";

export function OrderActions({ id, status, paymentStatus }: { id: string; status: string; paymentStatus: string }) {
  const router = useRouter();
  const btn = "rounded border px-3 py-1.5";
  const post = async (action: string) => {
    await fetch(`/api/orders/${id}/${action}`, { method: "POST" });
    router.refresh();
  };
  return (
    <div className="flex gap-2">
      {paymentStatus === "unpaid" && status !== "cancelled" && <button data-testid="order-pay-button" className={btn} onClick={() => post("pay")}>Pay (test mode)</button>}
      {status === "placed" && <button data-testid="order-cancel-button" className={btn} onClick={() => post("cancel")}>Cancel order</button>}
    </div>
  );
}
