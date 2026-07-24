import { auth } from "@clerk/nextjs/server";

import { Card } from "@/components/admin/Card";
import { DefRow } from "@/components/admin/DefRow";
import { EmptyState } from "@/components/admin/EmptyState";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { StatusChip } from "@/components/admin/StatusChip";
import { getProvisioning } from "@/app/admin/_lib/api";
import { formatRelativeTime } from "@/app/admin/_lib/time";

export const metadata = { title: "Provisioning · Admin" };

interface PageProps {
  params: Promise<{ id: string }>;
}

function RelativeTimestamp({ value }: { value: string | number | undefined | null }) {
  const when = formatRelativeTime(value);
  return <span title={when.iso ?? undefined}>{when.label}</span>;
}

/**
 * `provision_claimed_at` is stored as epoch **seconds**
 * (`int(time.time())` in `container_repo.py`), unlike every other timestamp
 * in this payload which is an ISO string. `formatRelativeTime` treats a raw
 * number as epoch milliseconds, so feeding it seconds directly would render
 * a bogus 1970 date. Render it as a precise, copyable ISO string instead —
 * matches the "mono, copyable text" treatment the rest of the lease fields
 * get.
 */
function formatEpochSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return new Date(value * 1000).toISOString();
}

function yesNoChip(value: boolean | null | undefined): React.ReactNode {
  if (value === null || value === undefined) return "—";
  return (
    <StatusChip status={value ? "running" : "stopped"} label={value ? "Yes" : "No"} />
  );
}

export default async function AdminUserProvisioningPage({ params }: PageProps) {
  const { id } = await params;

  const { getToken } = await auth();
  const token = await getToken();
  const data = token ? await getProvisioning(token, id) : null;

  if (!data) {
    return (
      <div className="space-y-6">
        <Header />
        <ErrorBanner error="Provisioning overview unreachable" variant="error" />
      </div>
    );
  }

  const statusContract = data.status_contract;
  const container = data.container;
  const containerReadError = container?.error ?? null;
  // Present exactly when a real containers row was read (not `null`, not the
  // `{error, source}` failure sentinel).
  const containerRow = containerReadError ? null : container;
  const paperclip = data.paperclip;
  const seededAgentIds = paperclip?.seeded_agent_ids ?? [];

  return (
    <div className="space-y-6">
      <Header />

      {/* 1. Readiness */}
      <Card title="Readiness">
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          <DefRow
            label="Runtime phase"
            value={<StatusChip status={statusContract?.runtime_phase ?? "unknown"} />}
          />
          <DefRow
            label="Lifecycle"
            value={<StatusChip status={statusContract?.lifecycle ?? "unknown"} />}
          />
          <DefRow label="Chat available" value={yesNoChip(statusContract?.chat_available)} />
          <DefRow
            label="Teams state"
            value={<StatusChip status={statusContract?.teams_state ?? "unknown"} />}
          />
        </dl>
      </Card>

      {/* 2. Container row */}
      <Card
        title="Container row"
        action={containerRow ? <StatusChip status={containerRow.status ?? "unknown"} /> : undefined}
      >
        {containerReadError ? (
          <ErrorBanner
            error={`Container row read failed: ${containerReadError}`}
            source={container?.source ?? "ddb_containers"}
            variant="error"
          />
        ) : containerRow ? (
          <>
            <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
              <DefRow label="Status" value={containerRow.status ?? "—"} />
              <DefRow label="Substatus" value={containerRow.substatus ?? "—"} />
              <DefRow label="Last error" value={containerRow.last_error ?? "—"} />
              <DefRow
                label="Last error at"
                value={<RelativeTimestamp value={containerRow.last_error_at} />}
                mono
              />
              <DefRow
                label="Retryable"
                value={
                  containerRow.retryable === null || containerRow.retryable === undefined
                    ? "—"
                    : containerRow.retryable
                      ? "Yes"
                      : "No"
                }
              />
            </dl>
            {containerRow.claim_stale ? (
              <div className="mt-3">
                <StatusChip status="degraded" label="STALE CLAIM" />
              </div>
            ) : null}
          </>
        ) : (
          <p className="text-sm text-zinc-500">No container row.</p>
        )}
      </Card>

      {/* 3. Lease / generations */}
      <Card title="Lease / generations">
        {containerRow ? (
          <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <DefRow
              label="Provision generation"
              value={containerRow.provision_generation ?? "—"}
              mono
            />
            <DefRow
              label="Provision claim ID"
              value={containerRow.provision_claim_id ?? "—"}
              mono
            />
            <DefRow
              label="Claimed at"
              value={formatEpochSeconds(containerRow.provision_claimed_at)}
              mono
            />
            <DefRow
              label="Attempt token"
              value={containerRow.provision_attempt_token ?? "—"}
              mono
            />
            <DefRow
              label="Execution name"
              value={containerRow.provision_execution_name ?? "—"}
              mono
            />
          </dl>
        ) : (
          <p className="text-sm text-zinc-500">No container row.</p>
        )}
      </Card>

      {/* 4. Paperclip company */}
      {paperclip ? (
        <Card
          title="Paperclip company"
          action={<StatusChip status={paperclip.status ?? "unknown"} />}
        >
          <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <DefRow label="Company ID" value={paperclip.company_id ?? "—"} mono />
            <DefRow
              label="Paperclip user ID"
              value={paperclip.paperclip_user_id ?? "—"}
              mono
            />
            <DefRow label="Last error" value={paperclip.last_error ?? "—"} />
            <DefRow
              label="Scheduled purge at"
              value={<RelativeTimestamp value={paperclip.scheduled_purge_at} />}
              mono
            />
          </dl>

          <div className="mt-4">
            <div className="mb-2 text-xs uppercase tracking-wide text-zinc-500">
              Seeded agents ({seededAgentIds.length})
            </div>
            {seededAgentIds.length > 0 ? (
              <ul className="space-y-1 text-sm">
                {seededAgentIds.map((agentId, index) => (
                  <li key={agentId} className="flex items-baseline gap-2">
                    <span className="truncate font-mono text-zinc-200">{agentId}</span>
                    {index === 0 ? (
                      <span className="text-xs text-zinc-500">Friday (CEO)</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-zinc-500">No seeded agents.</p>
            )}
          </div>
        </Card>
      ) : (
        <EmptyState
          title="No Paperclip company row"
          body="This owner has not been provisioned in Paperclip yet."
        />
      )}

      {/* 5. Onboarding */}
      <Card title="Onboarding">
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          <DefRow
            label="Funnel state"
            value={<StatusChip status={paperclip?.funnel_state ?? "unknown"} />}
          />
          <DefRow
            label="Bootstrap state"
            value={<StatusChip status={paperclip?.bootstrap_state ?? "unknown"} />}
          />
          <DefRow
            label="Bootstrap attempts"
            value={paperclip?.bootstrap_attempts?.toString() ?? "—"}
          />
          <DefRow label="Bootstrap last error" value={paperclip?.bootstrap_last_error ?? "—"} />
          <DefRow
            label="Dispatched at"
            value={<RelativeTimestamp value={paperclip?.bootstrap_dispatched_at} />}
            mono
          />
          <DefRow
            label="Delivered at"
            value={<RelativeTimestamp value={paperclip?.bootstrap_delivered_at} />}
            mono
          />
          <DefRow
            label="Onboarding generation"
            value={paperclip?.onboarding_generation ?? "—"}
            mono
          />
        </dl>
      </Card>

      {/* 6. Gate + SFN */}
      <Card title="Gate & SFN execution">
        <div>
          <div className="mb-2 text-xs uppercase tracking-wide text-zinc-500">Gate</div>
          {data.gate ? (
            <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
              <DefRow label="Code" value={data.gate.code ?? "—"} mono />
              <DefRow label="Title" value={data.gate.title ?? "—"} />
              <DefRow label="Message" value={data.gate.message ?? "—"} />
            </dl>
          ) : (
            <p className="text-sm text-zinc-500">Not blocked.</p>
          )}
        </div>

        <div className="mt-4 border-t border-zinc-800 pt-4">
          <div className="mb-2 text-xs uppercase tracking-wide text-zinc-500">
            SFN execution
          </div>
          {data.sfn_execution ? (
            <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
              <DefRow label="Name" value={data.sfn_execution.name ?? "—"} mono />
              <DefRow label="Status" value={data.sfn_execution.status ?? "—"} />
              <DefRow
                label="Start"
                value={<RelativeTimestamp value={data.sfn_execution.start_date} />}
                mono
              />
              <DefRow
                label="Stop"
                value={<RelativeTimestamp value={data.sfn_execution.stop_date} />}
                mono
              />
            </dl>
          ) : (
            <p className="text-sm text-zinc-500">No execution stamped.</p>
          )}
        </div>
      </Card>

      {/* Task 6 renders the levers panel (retry / reset-bootstrap) here. */}
    </div>
  );
}

// User-id breadcrumb is provided by the parent layout — render only the
// section heading here so the title doesn't duplicate.
function Header() {
  return <h1 className="text-xl font-semibold text-zinc-100">Provisioning</h1>;
}
