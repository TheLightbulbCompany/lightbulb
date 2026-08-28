import { auth } from "@clerk/nextjs/server";

import { Card } from "@/components/admin/Card";
import { DefRow } from "@/components/admin/DefRow";
import { EmptyState } from "@/components/admin/EmptyState";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { OrgBanner } from "@/components/admin/OrgBanner";
import { StatusChip } from "@/components/admin/StatusChip";
import { asRecord, asString } from "@/app/admin/_lib/narrow";
import {
  getOverview,
  listAvailableImages,
  type AvailableImage,
} from "@/app/admin/_lib/api";
import { formatRelativeTime } from "@/app/admin/_lib/time";
import { buildEcsConsoleUrl } from "@/lib/aws";

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

function RelativeTimestamp({ value }: { value: string | undefined }) {
  const when = formatRelativeTime(value);
  return <span title={when.iso ?? undefined}>{when.label}</span>;
}

export default async function AdminUserContainerPage({ params }: PageProps) {
  const { id } = await params;

  const { getToken } = await auth();
  const token = await getToken();
  const [overview, imagesPayload] = token
    ? await Promise.all([getOverview(token, id), listAvailableImages(token)])
    : [null, { images: [] as AvailableImage[] } as Awaited<ReturnType<typeof listAvailableImages>>];
  const availableImages = imagesPayload.images;
  const currentDevTag = imagesPayload.current_dev_tag;
  const currentProdTag = imagesPayload.current_prod_tag;

  if (!overview) {
    return (
      <div className="space-y-6">
        <Header />
        <ErrorBanner error="Container overview unreachable" variant="error" />
      </div>
    );
  }

  const orgBanner = overview.org ? (
    <OrgBanner
      org={overview.org}
      description="this container is the org’s shared resource."
    />
  ) : null;

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
      <Card
        title="Container"
        action={<StatusChip status={container.status ?? "unknown"} />}
      >
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
          <DefRow
            label="Created"
            value={<RelativeTimestamp value={container.created_at} />}
            mono
          />
          <DefRow
            label="Updated"
            value={<RelativeTimestamp value={container.updated_at} />}
            mono
          />
        </dl>
      </Card>

      {/* Actions */}
      <ContainerActionsPanel
        userId={id}
        availableImages={availableImages}
        currentDevTag={currentDevTag}
        currentProdTag={currentProdTag}
      />
    </div>
  );
}

// User-id breadcrumb is provided by the parent layout — render only the
// section heading here so the title doesn't duplicate.
function Header() {
  return <h1 className="text-xl font-semibold text-foreground">Container</h1>;
}

