interface OrgLike {
  id: string;
  slug: string;
  name: string;
  role: string;
}

interface Props {
  org: OrgLike;
  /**
   * Optional override for the trailing description line. Defaults to the
   * overview wording; container/agents sub-pages can pass their own
   * scoped message (e.g. "this container is the org's shared resource").
   */
  description?: string;
}

const DEFAULT_DESCRIPTION =
  "container, billing, and agents below are the org’s resources.";

/**
 * Info banner rendered at the top of admin user-detail views when the
 * target user is a member of a Clerk org. DDB rows are keyed by
 * `owner_id == org_id` in that case, so this makes the provenance
 * explicit to the admin.
 */
export function OrgBanner({ org, description = DEFAULT_DESCRIPTION }: Props) {
  const role = org.role ? org.role.replace("org:", "") : "member";
  const displayName = org.name || org.slug || org.id;
  return (
    <div className="rounded-md border-l-2 border-info bg-card px-4 py-3 text-sm">
      <div className="text-xs uppercase tracking-caps text-info">
        Org member
      </div>
      <div className="mt-1 text-foreground">
        {displayName}
        {org.slug ? (
          <span className="text-muted-foreground"> ({org.slug})</span>
        ) : null}
      </div>
      <div className="mt-1 text-xs text-muted-foreground">
        Role: {role} &mdash; {description}
      </div>
    </div>
  );
}
