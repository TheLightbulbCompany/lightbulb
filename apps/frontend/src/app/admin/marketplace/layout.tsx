import Link from "next/link";

/**
 * Layout shell for the /admin/marketplace tree.
 *
 * Sits inside the parent /admin layout (which already enforces auth + admin
 * gate via `getAdminMe`), so this layer only needs to render the secondary
 * sub-nav between the two moderation surfaces: the listings review queue and
 * the takedown queue. Both pages are Server Components further down the tree.
 */
export default function MarketplaceAdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div>
      <nav className="mb-6 flex gap-6 border-b border-border">
        <Link
          href="/admin/marketplace/listings"
          className="pb-3 text-sm text-muted-foreground hover:text-foreground"
        >
          Review queue
        </Link>
        <Link
          href="/admin/marketplace/takedowns"
          className="pb-3 text-sm text-muted-foreground hover:text-foreground"
        >
          Takedowns
        </Link>
      </nav>
      {children}
    </div>
  );
}
