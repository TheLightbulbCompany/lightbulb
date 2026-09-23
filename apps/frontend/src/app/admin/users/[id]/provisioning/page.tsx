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
  const starterAgentIds = Object.entries(paperclipRow?.starter_agent_ids ?? {});
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
        ) : (
          <p className="text-sm text-muted-foreground">No container row.</p>
        )}
      </Card>

      {/* 3. Paperclip company */}
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
            <div className="mb-2 text-xs uppercase tracking-caps text-muted-foreground">
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
          <div className="mt-4">
            <div className="mb-2 text-xs uppercase tracking-caps text-muted-foreground">
              Starter agents ({starterAgentIds.length})
            </div>
            {starterAgentIds.length > 0 ? (
              <ul className="space-y-1 text-sm">
                {starterAgentIds.map(([role, agentId]) => (
                  <li key={role} className="flex items-baseline gap-2">
                    <span className="text-xs text-muted-foreground">{role}</span>
                    <span className="truncate font-mono text-foreground">{agentId}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">No starter agents.</p>
            )}
          </div>
        </Card>
      ) : (
        <EmptyState
          title="No Paperclip company row"
          body="This owner has not been provisioned in Paperclip yet."
        />
      )}

      {/* 4. Onboarding */}
      <Card title="Onboarding">
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          <DefRow
            label="Funnel state"
            value={<StatusChip status={paperclipRow?.funnel_state ?? "unknown"} />}
          />
          <DefRow
            label="Onboarding generation"
            value={paperclipRow?.onboarding_generation ?? "—"}
            mono
          />
        </dl>
      </Card>

      {/* 5. Gate + SFN */}
      <Card title="Gate & SFN execution">
        <div>
          <div className="mb-2 text-xs uppercase tracking-caps text-muted-foreground">Gate</div>
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
          <div className="mb-2 text-xs uppercase tracking-caps text-muted-foreground">
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

      {/* 6. Levers */}
      <ProvisioningActionsPanel
        userId={id}
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
