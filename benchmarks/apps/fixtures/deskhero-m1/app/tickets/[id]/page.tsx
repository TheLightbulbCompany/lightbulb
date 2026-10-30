import { notFound } from "next/navigation";
import { TicketActions } from "@/components/ticket-actions";
import { requireUser } from "@/lib/auth";
import { getTicket } from "@/lib/tickets";

export default async function TicketPage({ params }: { params: Promise<{ id: string }> }) {
  const user = await requireUser();
  const t = await getTicket(user.id, (await params).id);
  if (!t) notFound();
  return (
    <div className="space-y-3">
      <h1 data-testid="ticket-detail-subject" className="text-xl font-semibold">{t.subject}</h1>
      <p>Status: <span data-testid="ticket-detail-status">{t.status}</span></p>
      <p>Priority: <span data-testid="ticket-detail-priority">{t.priority}</span></p>
      {t.body && <p className="whitespace-pre-wrap">{t.body}</p>}
      <TicketActions ticket={{ id: t.id, subject: t.subject, body: t.body, priority: t.priority, status: t.status }} />
    </div>
  );
}
