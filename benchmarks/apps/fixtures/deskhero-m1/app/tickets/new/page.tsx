import { TicketForm } from "@/components/ticket-form";
import { requireUser } from "@/lib/auth";

export default async function NewTicket() {
  await requireUser();
  return (
    <div className="space-y-3">
      <h1 className="text-xl font-semibold">New ticket</h1>
      <TicketForm />
    </div>
  );
}
