import { auth } from "@clerk/nextjs/server";

import { Card } from "@/components/admin/Card";
import { DefRow } from "@/components/admin/DefRow";
import { EmptyState } from "@/components/admin/EmptyState";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { StatusChip } from "@/components/admin/StatusChip";
import { getProvisioning } from "@/app/admin/_lib/api";
import { formatRelativeTime } from "@/app/admin/_lib/time";
import { ProvisioningActionsPanel } from "./ProvisioningActionsPanel";

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
  const paperclipReadError = paperclip?.error ?? null;
  // Present exactly when a real Paperclip-company row was read (not `null`,
  // not the `{error, source}` failure sentinel).
  const paperclipRow = paperclipReadError ? null : paperclip;
  const seededAgentIds = paperclipRow?.seeded_agent_ids ?? [];
  const sfnExecution = data.sfn_execution;
  const sfnReadError = sfnExecution?.error ?? null;
  // Present exactly when a real DescribeExecution response was read (not
  // `null`, not the `{error, source}` failure sentinel).
  const sfnRow = sfnReadError ? null : sfnExecution;

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
          <p className="text-sm text-muted-foreground">No container row.</p>
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
          <p className="text-sm text-muted-foreground">No container row.</p>
        )}
      </Card>

      {/* 4. Paperclip company */}
      {paperclipReadError ? (
        <Card title="Paperclip company">
          <ErrorBanner
            error={`Paperclip company read failed: ${paperclipReadError}`}
            source={paperclip?.source ?? "ddb_paperclip"}
            variant="error"
          />
        </Card>
      ) : paperclipRow ? (
        <Card
          title="Paperclip company"
          action={<StatusChip status={paperclipRow.status ?? "unknown"} />}
        >
          <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <DefRow label="Company ID" value={paperclipRow.company_id ?? "—"} mono />
            <DefRow
              label="Paperclip user ID"
              value={paperclipRow.paperclip_user_id ?? "—"}
              mono
            />
            <DefRow label="Last error" value={paperclipRow.last_error ?? "—"} />
            <DefRow
              label="Scheduled purge at"
              value={<RelativeTimestamp value={paperclipRow.scheduled_purge_at} />}
              mono
            />
          </dl>

          <div className="mt-4">
            <div className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">
              Seeded agents ({seededAgentIds.length})
            </div>
            {seededAgentIds.length > 0 ? (
              <ul className="space-y-1 text-sm">
                {seededAgentIds.map((agentId, index) => (
                  <li key={agentId} className="flex items-baseline gap-2">
                    <span className="truncate font-mono text-foreground">{agentId}</span>
                    {index === 0 ? (
                      <span className="text-xs text-muted-foreground">Friday (CEO)</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">No seeded agents.</p>
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
            value={<StatusChip status={paperclipRow?.funnel_state ?? "unknown"} />}
          />
          <DefRow
            label="Bootstrap state"
            value={<StatusChip status={paperclipRow?.bootstrap_state ?? "unknown"} />}
          />
          <DefRow
            label="Bootstrap attempts"
            value={paperclipRow?.bootstrap_attempts?.toString() ?? "—"}
          />
          <DefRow label="Bootstrap last error" value={paperclipRow?.bootstrap_last_error ?? "—"} />
          <DefRow
            label="Dispatched at"
            value={<RelativeTimestamp value={paperclipRow?.bootstrap_dispatched_at} />}
            mono
          />
          <DefRow
            label="Send attempted at"
            value={<RelativeTimestamp value={paperclipRow?.bootstrap_send_attempted_at} />}
            mono
          />
          <DefRow
            label="Sent at"
            value={<RelativeTimestamp value={paperclipRow?.bootstrap_sent_at} />}
            mono
          />
          <DefRow
            label="Delivered at"
            value={<RelativeTimestamp value={paperclipRow?.bootstrap_delivered_at} />}
            mono
          />
          <DefRow
            label="Onboarding generation"
            value={paperclipRow?.onboarding_generation ?? "—"}
            mono
          />
        </dl>
      </Card>

      {/* 6. Gate + SFN */}
      <Card title="Gate & SFN execution">
        <div>
          <div className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">Gate</div>
          {data.gate ? (
            <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
              <DefRow label="Code" value={data.gate.code ?? "—"} mono />
              <DefRow label="Title" value={data.gate.title ?? "—"} />
              <DefRow label="Message" value={data.gate.message ?? "—"} />
            </dl>
          ) : (
            <p className="text-sm text-muted-foreground">Not blocked.</p>
          )}
        </div>

        <div className="mt-4 border-t border-border pt-4">
          <div className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">
            SFN execution
          </div>
          {sfnReadError ? (
            <ErrorBanner
              error={`SFN execution read failed: ${sfnReadError}`}
              source={sfnExecution?.source ?? "sfn_execution"}
              variant="error"
            />
          ) : sfnRow ? (
            <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
              <DefRow label="Name" value={sfnRow.name ?? "—"} mono />
              <DefRow label="Status" value={sfnRow.status ?? "—"} />
              <DefRow
                label="Start"
                value={<RelativeTimestamp value={sfnRow.start_date} />}
                mono
              />
              <DefRow
                label="Stop"
                value={<RelativeTimestamp value={sfnRow.stop_date} />}
                mono
              />
            </dl>
          ) : (
            <p className="text-sm text-muted-foreground">No execution stamped.</p>
          )}
        </div>
      </Card>

      {/* 7. Levers */}
      <ProvisioningActionsPanel
        userId={id}
        bootstrapState={paperclipRow?.bootstrap_state ?? null}
        containerStatus={containerRow?.status ?? null}
      />
    </div>
  );
}

// User-id breadcrumb is provided by the parent layout — render only the
// section heading here so the title doesn't duplicate.
function Header() {
  return <h1 className="text-xl font-semibold text-foreground">Provisioning</h1>;
}
