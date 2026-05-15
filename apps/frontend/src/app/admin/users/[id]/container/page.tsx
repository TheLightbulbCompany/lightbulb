import { auth } from "@clerk/nextjs/server";

import { EmptyState } from "@/components/admin/EmptyState";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import {
  getOverview,
  listAvailableImages,
  type AdminOrgContext,
  type AvailableImage,
} from "@/app/admin/_lib/api";

import { ContainerActionsPanel } from "./ContainerActionsPanel";

export const metadata = { title: "Container \u00b7 Admin" };

interface PageProps {
  params: Promise<{ id: string }>;
}

interface ContainerSlice {
  status?: string;
  task_arn?: string;
  service_name?: string;
  cluster_name?: string;
  cluster_arn?: string;
  access_point_id?: string;
  subscription_status?: string | null;
  created_at?: string;
  updated_at?: string;
  error?: string;
  source?: string;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function pickContainer(raw: unknown): { container: ContainerSlice | null; raw: Record<string, unknown> } {
  const r = asRecord(raw);
  if (Object.keys(r).length === 0) return { container: null, raw: r };
  // Backend may return `{ error, source }` on timeout — surface as a slice
  // with no other fields so the page can still render the error banner.
  return {
    container: {
      status: asString(r.status),
      task_arn: asString(r.task_arn),
      service_name: asString(r.service_name),
      cluster_name: asString(r.cluster_name),
      cluster_arn: asString(r.cluster_arn),
      access_point_id: asString(r.access_point_id),
      subscription_status: asString(r.subscription_status),
      created_at: asString(r.created_at),
      updated_at: asString(r.updated_at),
      error: asString(r.error),
      source: asString(r.source),
    },
    raw: r,
  };
}

function statusChipClasses(status?: string): string {
  switch (status) {
    case "running":
      return "bg-emerald-500/15 text-emerald-300 border-emerald-500/30";
    case "provisioning":
    case "starting":
      return "bg-sky-500/15 text-sky-300 border-sky-500/30";
    case "stopped":
    case "scaled_to_zero":
      return "bg-amber-500/15 text-amber-300 border-amber-500/30";
    case "error":
    case "failed":
    case "unreachable":
      return "bg-red-500/15 text-red-300 border-red-500/30";
    default:
      return "bg-zinc-500/15 text-zinc-300 border-zinc-500/30";
  }
}

/**
 * Build an ECS console deep-link for the user's container task.
 *
 * The cluster name component of the URL is best-effort: backend rows store
 * just `cluster_arn` in some cases. We extract the trailing segment when an
 * ARN is present and fall back to a generic console link otherwise so the
 * operator can still pivot.
 */
function buildEcsConsoleUrl(c: ContainerSlice): string | null {
  if (!c.task_arn) return null;
  const region = "us-east-1";
  const cluster =
    c.cluster_name ??
    (c.cluster_arn ? c.cluster_arn.split("/").pop() : undefined);
  if (cluster && c.service_name) {
    return `https://${region}.console.aws.amazon.com/ecs/v2/clusters/${encodeURIComponent(cluster)}/services/${encodeURIComponent(c.service_name)}/tasks?region=${region}`;
  }
  return `https://${region}.console.aws.amazon.com/ecs/v2/clusters?region=${region}`;
}

export default async function AdminUserContainerPage({ params }: PageProps) {
  const { id } = await params;

  const { getToken } = await auth();
  const token = await getToken();
  const [overview, availableImages] = token
    ? await Promise.all([
        getOverview(token, id),
        listAvailableImages(token).then((r) => r.images),
      ])
    : [null, [] as AvailableImage[]];

  if (!overview) {
    return (
      <div className="space-y-6">
        <Header />
        <ErrorBanner error="Container overview unreachable" variant="error" />
      </div>
    );
  }

  const orgBanner = overview.org ? <OrgBanner org={overview.org} /> : null;

  const { container } = pickContainer(overview.container);

  if (!container) {
    return (
      <div className="space-y-6">
        <Header />
        {orgBanner}
        <EmptyState
          title="No container"
          body="This user has no provisioned container."
        />
      </div>
    );
  }

  const ecsUrl = buildEcsConsoleUrl(container);

  return (
    <div className="space-y-6">
      <Header />
      {orgBanner}

      {container.error ? (
        <ErrorBanner
          error={container.error}
          source={container.source ?? "DynamoDB"}
          variant="error"
        />
      ) : null}

      {/* Container metadata */}
      <section className="space-y-3 rounded-md border border-white/10 bg-white/[0.02] p-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-400">
            Container
          </h2>
          <span
            className={`rounded border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${statusChipClasses(container.status)}`}
          >
            {container.status ?? "unknown"}
          </span>
        </div>
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          <DefRow label="Subscription" value={container.subscription_status ?? "\u2014"} />
          <DefRow label="Service name" value={container.service_name ?? "\u2014"} mono />
          <DefRow
            label="Task ARN"
            value={container.task_arn ?? "\u2014"}
            mono
            href={ecsUrl ?? undefined}
          />
          <DefRow
            label="Access point"
            value={container.access_point_id ?? "\u2014"}
            mono
          />
          <DefRow label="Created" value={container.created_at ?? "\u2014"} mono />
          <DefRow label="Updated" value={container.updated_at ?? "\u2014"} mono />
        </dl>
      </section>

      {/* Actions */}
      <ContainerActionsPanel userId={id} availableImages={availableImages} />
    </div>
  );
}

// User-id breadcrumb is provided by the parent layout — render only the
// section heading here so the title doesn't duplicate.
function Header() {
  return <h1 className="text-xl font-semibold text-zinc-100">Container</h1>;
}

/**
 * Indigo banner rendered when the target user belongs to a Clerk org.
 * Mirrors the overview page so admins know this container is the org's
 * shared resource (owner_id == org_id), not the individual user's.
 */
function OrgBanner({ org }: { org: AdminOrgContext }) {
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
        Role: {role} &mdash; this container is the org&apos;s shared resource.
      </div>
    </div>
  );
}

function DefRow({
  label,
  value,
  mono,
  href,
}: {
  label: string;
  value: string;
  mono?: boolean;
  href?: string;
}) {
  const cls = mono ? "truncate font-mono text-zinc-200" : "truncate text-zinc-200";
  return (
    <div className="flex items-baseline gap-3">
      <dt className="w-32 shrink-0 text-xs uppercase tracking-wide text-zinc-500">
        {label}
      </dt>
      <dd className="min-w-0 flex-1">
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className={`block ${cls} text-sky-300 hover:underline`}
            title={value}
          >
            {value}
          </a>
        ) : (
          <span className={cls} title={value}>
            {value}
          </span>
        )}
      </dd>
    </div>
  );
}
