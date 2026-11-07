import { NotAuthorized } from "@/components/not-authorized";
import { requireUser } from "@/lib/auth";
import { listMembers, membership } from "@/lib/data";

export default async function Members({ params }: { params: Promise<{ orgId: string }> }) {
  const user = await requireUser();
  const m = await membership(user.id, (await params).orgId);
  if (!m) return <NotAuthorized />;
  const rows = await listMembers(m.org.id);
  return (
    <div className="space-y-3">
      <p data-testid="member-count">{rows.length} {rows.length === 1 ? "member" : "members"}</p>
      <table data-testid="members-table" className="w-full rounded border bg-white text-sm">
        <thead><tr className="text-left"><th className="p-2">Email</th><th className="p-2">Role</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.userId} data-testid="member-row" data-member-email={r.email} data-user-id={r.userId} className="border-t">
              <td data-testid="member-email" className="p-2">{r.email}</td>
              <td data-testid="member-role" className="p-2">{r.role}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
