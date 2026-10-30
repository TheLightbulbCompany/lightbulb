import { notFound } from "next/navigation";
import { OrderActions } from "@/components/order-actions";
import { requireUser } from "@/lib/auth";
import { getOrder } from "@/lib/data";
import { dollars } from "@/lib/money";

export default async function OrderPage({ params }: { params: Promise<{ id: string }> }) {
  const user = await requireUser();
  const o = await getOrder(user.id, (await params).id);
  if (!o) notFound();
  return (
    <div className="space-y-3">
      <h1 className="text-xl font-semibold">{o.restaurantName}</h1>
      <ul className="divide-y rounded border bg-white">
        {o.lines.map((l) => (
          <li key={l.id} data-testid="order-line" className="flex justify-between px-4 py-2">
            <span>{l.name} × {l.quantity}</span>
            <span data-cents={l.lineCents}>{dollars(l.lineCents)}</span>
          </li>
        ))}
      </ul>
      <p>Subtotal: <span data-testid="order-subtotal" data-cents={o.subtotalCents}>{dollars(o.subtotalCents)}</span></p>
      <p>Total: <span data-testid="order-total" data-cents={o.totalCents}>{dollars(o.totalCents)}</span></p>
      <p>Status: <span data-testid="order-detail-status">{o.status}</span></p>
      <p>Payment: <span data-testid="order-payment-status">{o.paymentStatus}</span></p>
      <OrderActions id={o.id} status={o.status} paymentStatus={o.paymentStatus} />
    </div>
  );
}
