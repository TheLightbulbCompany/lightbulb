import { auth } from "@clerk/nextjs/server";

import { Card } from "@/components/admin/Card";
import { DefRow } from "@/components/admin/DefRow";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { OrgBanner } from "@/components/admin/OrgBanner";
import { StatusChip } from "@/components/admin/StatusChip";
import { getOverview } from "@/app/admin/_lib/api";
import { formatRelativeTime } from "@/app/admin/_lib/time";

export const metadata = { title: "Overview · Admin" };

// ---------------------------------------------------------------------------
// Section payload shapes (narrowed locally — `UserOverview.identity` etc.
// are typed as `unknown` in the shared API client so the SC stays robust if
// the backend evolves).
// ---------------------------------------------------------------------------

interface ErrorPayload {
  error?: string;
  source?: string;
}

interface ClerkIdentity extends ErrorPayload {
  id?: string;
  email_addresses?: Array<{ email_address?: string }>;
  first_name?: string | null;
  last_name?: string | null;
  created_at?: number | string | null;
  last_sign_in_at?: number | string | null;
  banned?: boolean;
}

interface BillingRow extends ErrorPayload {
  subscription_status?: string | null;
  stripe_customer_id?: string;
  stripe_subscription_id?: string | null;
  overage_enabled?: boolean;
}

interface ContainerRow extends ErrorPayload {
  status?: string;
  agentcore_runtime_arn?: string | null;
  subscription_status?: string | null;
  created_at?: number | string | null;
}

interface UsageRow extends ErrorPayload {
  total_spend_microdollars?: number;
  total_input_tokens?: number;
  total_output_tokens?: number;
  total_cache_read_tokens?: number;
  total_cache_write_tokens?: number;
  period?: string;
}

function hasError(
  value: unknown,
): value is { error: string; source?: string } {
  return (
    typeof value === "object" &&
    value !== null &&
    "error" in value &&
    typeof (value as { error?: unknown }).error === "string"
  );
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function RelativeTimestamp({ value }: { value: number | string | null | undefined }) {
  const when = formatRelativeTime(value);
  return (
    <span className="font-mono text-xs" title={when.iso ?? undefined}>
      {when.label}
    </span>
  );
}

/**
 * Format microdollars as USD. 1_000_000 microdollars = $1.00.
 * `microdollars` here is the unit used by usage_repo (CEO billing convention).
 */
function formatUsd(microdollars?: number): string {
  if (microdollars === undefined || microdollars === null) return "$0.00";
  const dollars = microdollars / 1_000_000;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(dollars);
}

interface UserOverviewPageProps {
  params: Promise<{ id: string }>;
}

export default async function UserOverviewPage({ params }: UserOverviewPageProps) {
  const { id } = await params;
  const { getToken } = await auth();
  const token = await getToken();
  const overview = token ? await getOverview(token, id) : null;

  if (!overview) {
    return (
      <ErrorBanner error="Overview endpoint unreachable" variant="error" />
    );
  }

  return (
    <div className="space-y-4">
      {overview.org ? <OrgBanner org={overview.org} /> : null}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <IdentityCard identity={overview.identity} />
        <BillingCard billing={overview.billing} />
        <ContainerCard container={overview.container} />
        <UsageCard usage={overview.usage} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------

function Rows({ children }: { children: React.ReactNode }) {
  return <dl className="grid grid-cols-1 gap-y-2 text-sm">{children}</dl>;
}

function None() {
  return <span className="text-muted-foreground">(none)</span>;
}

function IdentityCard({ identity }: { identity: unknown }) {
  if (!isPlainObject(identity)) {
    return (
      <Card title="Identity">
        <ErrorBanner error="No identity data available." source="Clerk" />
      </Card>
    );
  }
  if (hasError(identity)) {
    return (
      <Card title="Identity">
        <ErrorBanner error={identity.error ?? "Unknown error"} source="Clerk" />
      </Card>
    );
  }

  const data = identity as ClerkIdentity;
  const email = data.email_addresses?.[0]?.email_address ?? null;
  const fullName = [data.first_name, data.last_name].filter(Boolean).join(" ");

  return (
    <Card title="Identity">
      <Rows>
        <DefRow label="Email" value={email ?? <None />} />
        <DefRow label="Name" value={fullName || <None />} />
        <DefRow label="Created" value={<RelativeTimestamp value={data.created_at} />} />
        <DefRow
          label="Last sign-in"
          value={<RelativeTimestamp value={data.last_sign_in_at} />}
        />
        <DefRow
          label="Banned"
          value={
            data.banned ? (
              <StatusChip status="error" label="Yes" />
            ) : (
              <span className="text-xs text-muted-foreground">No</span>
            )
          }
        />
      </Rows>
    </Card>
  );
}

function BillingCard({ billing }: { billing: unknown }) {
  if (!isPlainObject(billing)) {
    return (
      <Card title="Billing">
        <p className="text-sm text-muted-foreground">No billing record.</p>
      </Card>
    );
  }
  if (hasError(billing)) {
    return (
      <Card title="Billing">
        <ErrorBanner
          error={billing.error ?? "Unknown error"}
          source={billing.source ?? "DynamoDB"}
        />
      </Card>
    );
  }

  const data = billing as BillingRow;
  return (
    <Card title="Billing">
      <Rows>
        <DefRow label="Plan" value={data.subscription_status ?? <None />} />
        <DefRow
          label="Stripe customer"
          value={data.stripe_customer_id ?? <None />}
          mono
          href={
            data.stripe_customer_id
              ? `https://dashboard.stripe.com/customers/${encodeURIComponent(data.stripe_customer_id)}`
              : undefined
          }
        />
        <DefRow
          label="Subscription"
          value={data.stripe_subscription_id ?? <None />}
          mono
        />
      </Rows>
    </Card>
  );
}

function ContainerCard({ container }: { container: unknown }) {
  if (!isPlainObject(container)) {
    return (
      <Card title="Container">
        <p className="text-sm text-muted-foreground">No container provisioned yet.</p>
      </Card>
    );
  }
  if (hasError(container)) {
    return (
      <Card title="Container">
        <ErrorBanner
          error={container.error ?? "Unknown error"}
          source={container.source ?? "DynamoDB"}
        />
      </Card>
    );
  }

  const data = container as ContainerRow;
  return (
    <Card title="Container">
      <Rows>
        <DefRow
          label="Status"
          value={<StatusChip status={data.status ?? "unknown"} />}
        />
        <DefRow label="Plan" value={data.subscription_status ?? <None />} />
        {/* No console deep-link: an AgentCore runtime has no per-workspace console page the
            way an ECS service did, so the ARN is what an operator pastes into
            `aws bedrock-agentcore get-agent-runtime`. */}
        <DefRow label="Runtime" value={data.agentcore_runtime_arn ?? <None />} mono />
        <DefRow label="Created" value={<RelativeTimestamp value={data.created_at} />} />
      </Rows>
    </Card>
  );
}

function UsageCard({ usage }: { usage: unknown }) {
  if (!isPlainObject(usage)) {
    return (
      <Card title="Usage">
        <p className="text-sm text-muted-foreground">No usage data.</p>
      </Card>
    );
  }
  if (hasError(usage)) {
    return (
      <Card title="Usage">
        <ErrorBanner
          error={usage.error ?? "Unknown error"}
          source={usage.source ?? "DynamoDB"}
        />
      </Card>
    );
  }

  const data = usage as UsageRow;
  const totalTokens =
    (data.total_input_tokens ?? 0) +
    (data.total_output_tokens ?? 0) +
    (data.total_cache_read_tokens ?? 0) +
    (data.total_cache_write_tokens ?? 0);

  return (
    <Card title="Usage">
      <Rows>
        <DefRow label="Period" value={data.period ?? "current"} />
        <DefRow label="Spend" value={formatUsd(data.total_spend_microdollars)} mono />
        <DefRow label="Tokens (total)" value={totalTokens.toLocaleString()} mono />
        <DefRow
          label="Input tokens"
          value={(data.total_input_tokens ?? 0).toLocaleString()}
          mono
        />
        <DefRow
          label="Output tokens"
          value={(data.total_output_tokens ?? 0).toLocaleString()}
          mono
        />
      </Rows>
    </Card>
  );
}
