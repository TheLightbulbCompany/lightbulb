import type { ReactNode } from "react";
import Link from "next/link";
import { requireUser } from "@/lib/auth";
import { membership } from "@/lib/data";

/** The org header. Each page below checks membership itself too: layouts do not guard their pages. */
export default async function OrgLayout({ children, params }: { children: ReactNode; params: Promise<{ orgId: string }> }) {
  const user = await requireUser();
  const { orgId } = await params;
  const m = await membership(user.id, orgId);
  if (!m) return <>{children}</>; // the page shows the not-authorised element
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between border-b pb-3">
        <Link href={`/orgs/${orgId}`}><h1 data-testid="org-header-name" className="text-xl font-semibold">{m.org.name}</h1></Link>
        <nav className="flex gap-4 text-sm">
          <Link href={`/orgs/${orgId}/settings`} data-testid="nav-settings" className="underline">Settings</Link>
          <Link href={`/orgs/${orgId}/members`} data-testid="nav-members" className="underline">Members</Link>
        </nav>
      </div>
      {children}
    </div>
  );
}
