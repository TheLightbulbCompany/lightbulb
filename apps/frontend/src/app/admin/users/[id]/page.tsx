import { auth } from "@clerk/nextjs/server";

import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { OrgBanner } from "@/components/admin/OrgBanner";
import { getOverview } from "@/app/admin/_lib/api";

export const metadata = { title: "Overview \u00b7 Admin" };

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
  task_arn?: string | null;
  service_name?: string | null;
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

const CONTAINER_STATUS_CLASSES: Record<string, string> = {
  running: "bg-emerald-500/15 text-emerald-300",
  provisioning: "bg-sky-500/15 text-sky-300",
  stopped: "bg-zinc-500/15 text-zinc-300",
  error: "bg-red-500/15 text-red-300",
};

function statusClass(status?: string): string {
  if (!status) return "bg-zinc-500/15 text-zinc-400";
  return CONTAINER_STATUS_CLASSES[status] ?? "bg-zinc-500/15 text-zinc-400";
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

function formatDateValue(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "\u2014";
  if (typeof value === "number") {
    try {
      return new Date(value).toISOString().replace("T", " ").slice(0, 19) + "Z";
    } catch {
      return String(value);
    }
  }
  return value;
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

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-md border border-white/10 bg-white/[0.02] p-4">
      <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-zinc-400">
        {title}
      </h2>
      {children}
    </section>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 border-t border-white/5 py-1.5 first:border-t-0">
      <span className="text-xs text-zinc-400">{label}</span>
      <span className="max-w-[60%] truncate text-right text-sm text-zinc-100">
        {value}
      </span>
    </div>
  );
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
      <div className="space-y-0">
        <Field
          label="Email"
          value={email ?? <span className="text-zinc-500">(none)</span>}
        />
        <Field
          label="Name"
          value={fullName || <span className="text-zinc-500">(none)</span>}
        />
        <Field label="Created" value={<span className="font-mono text-xs">{formatDateValue(data.created_at)}</span>} />
        <Field
          label="Last sign-in"
          value={<span className="font-mono text-xs">{formatDateValue(data.last_sign_in_at)}</span>}
        />
        <Field
          label="Banned"
          value={
            data.banned ? (
              <span className="rounded bg-red-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-red-300">
                Yes
              </span>
            ) : (
              <span className="text-xs text-zinc-500">No</span>
            )
          }
        />
      </div>
    </Card>
  );
}

function BillingCard({ billing }: { billing: unknown }) {
  if (!isPlainObject(billing)) {
    return (
      <Card title="Billing">
        <p className="text-sm text-zinc-500">No billing record.</p>
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
      <div className="space-y-0">
        <Field label="Plan" value={data.subscription_status ?? <span className="text-zinc-500">(none)</span>} />
        <Field
          label="Stripe customer"
          value={
            data.stripe_customer_id ? (
              <a
                href={`https://dashboard.stripe.com/customers/${encodeURIComponent(data.stripe_customer_id)}`}
                target="_blank"
                rel="noopener noreferrer"
                className="font-mono text-xs text-sky-300 hover:underline"
                title={data.stripe_customer_id}
              >
                {data.stripe_customer_id}
              </a>
            ) : (
              <span className="text-zinc-500">(none)</span>
            )
          }
        />
        <Field
          label="Subscription"
          value={
            data.stripe_subscription_id ? (
              <span className="font-mono text-xs" title={data.stripe_subscription_id}>
                {data.stripe_subscription_id}
              </span>
            ) : (
              <span className="text-zinc-500">(none)</span>
            )
          }
        />
      </div>
    </Card>
  );
}

function ContainerCard({ container }: { container: unknown }) {
  if (!isPlainObject(container)) {
    return (
      <Card title="Container">
        <p className="text-sm text-zinc-500">No container provisioned yet.</p>
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
      <div className="space-y-0">
        <Field
          label="Status"
          value={
            <span
              className={`rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${statusClass(data.status)}`}
            >
              {data.status ?? "unknown"}
            </span>
          }
        />
        <Field label="Plan" value={data.subscription_status ?? <span className="text-zinc-500">(none)</span>} />
        <Field
          label="Service"
          value={
            data.service_name ? (
              <span className="font-mono text-xs" title={data.service_name}>
                {data.service_name}
              </span>
            ) : (
              <span className="text-zinc-500">(none)</span>
            )
          }
        />
        <Field
          label="Task ARN"
          value={
            data.task_arn ? (
              <span className="font-mono text-xs" title={data.task_arn}>
                {data.task_arn}
              </span>
            ) : (
              <span className="text-zinc-500">(none)</span>
            )
          }
        />
        <Field
          label="Created"
          value={<span className="font-mono text-xs">{formatDateValue(data.created_at)}</span>}
        />
      </div>
    </Card>
  );
}

function UsageCard({ usage }: { usage: unknown }) {
  if (!isPlainObject(usage)) {
    return (
      <Card title="Usage">
        <p className="text-sm text-zinc-500">No usage data.</p>
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
      <div className="space-y-0">
        <Field
          label="Period"
          value={data.period ?? <span className="text-zinc-500">current</span>}
        />
        <Field
          label="Spend"
          value={<span className="font-mono">{formatUsd(data.total_spend_microdollars)}</span>}
        />
        <Field
          label="Tokens (total)"
          value={<span className="font-mono">{totalTokens.toLocaleString()}</span>}
        />
        <Field
          label="Input tokens"
          value={<span className="font-mono">{(data.total_input_tokens ?? 0).toLocaleString()}</span>}
        />
        <Field
          label="Output tokens"
          value={<span className="font-mono">{(data.total_output_tokens ?? 0).toLocaleString()}</span>}
        />
      </div>
    </Card>
  );
}
