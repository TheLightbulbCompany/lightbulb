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
 * Indigo banner rendered at the top of admin user-detail views when the
 * target user is a member of a Clerk org. DDB rows are keyed by
 * `owner_id == org_id` in that case, so this makes the provenance
 * explicit to the admin.
 */
export function OrgBanner({ org, description = DEFAULT_DESCRIPTION }: Props) {
  const role = org.role ? org.role.replace("org:", "") : "member";
  const displayName = org.name || org.slug || org.id;
  return (
    <div className="rounded-md border border-indigo-800 bg-indigo-950/30 px-4 py-3 text-sm">
      <div className="text-xs uppercase tracking-wide text-indigo-400">
        Org member
      </div>
      <div className="mt-1 text-indigo-200">
        {displayName}
        {org.slug ? (
          <span className="text-indigo-500"> ({org.slug})</span>
        ) : null}
      </div>
      <div className="mt-1 text-xs text-indigo-400">
        Role: {role} &mdash; {description}
      </div>
    </div>
  );
}
