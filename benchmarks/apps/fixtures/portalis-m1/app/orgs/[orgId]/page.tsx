import { NotAuthorized } from "@/components/not-authorized";
import { requireUser } from "@/lib/auth";
import { membership } from "@/lib/data";

export default async function Overview({ params }: { params: Promise<{ orgId: string }> }) {
  const user = await requireUser();
  const m = await membership(user.id, (await params).orgId);
  if (!m) return <NotAuthorized />;
  return (
    <div className="space-y-2">
      <p className="text-slate-600">{m.org.description || "No description yet."}</p>
      <p className="text-sm text-slate-500">Your role: {m.role}</p>
    </div>
  );
}
