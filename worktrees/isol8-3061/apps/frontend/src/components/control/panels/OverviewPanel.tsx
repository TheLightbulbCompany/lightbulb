"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  Loader2,
  RefreshCw,
  Wifi,
  WifiOff,
  Clock,
  Server,
  Users,
  MapPin,
  CreditCard,
  Activity,
  Wrench,
  FileText,
} from "lucide-react";
import { useStatusLoaded } from "@lightbulb/shared";
import { useRuntimeRpc } from "@/hooks/useRuntimeRpc";
import { useSystemHealth } from "@/hooks/useSystemHealth";
import { useProvisioningState } from "@/hooks/useProvisioningState";
import { useBilling } from "@/hooks/useBilling";
import { planLabel, isFreeTierExpired } from "@/lib/planLabel";
import { useApi } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface HealthAgent {
  agentId?: string;
  isDefault?: boolean;
  sessions?: { count?: number; recent?: unknown[] };
  [key: string]: unknown;
}

interface HealthPayload {
  ok?: boolean;
  // The Hermes bridge's health payload carries `ready`, not `ok` (`apps/infra/hermes/bridge/server.py::_health`) —
  // see `isOnline` below, which reads both the same way `container_recover._health_verdict` does.
  ready?: boolean;
  ts?: number;
  durationMs?: number;
  channels?: Record<string, unknown>;
  defaultAgentId?: string;
  // The Hermes bridge sends a plain count (and `uptimeS` instead of a boot `ts`);
  // an array is still accepted — see `healthCount`.
  agents?: HealthAgent[] | number;
  uptimeS?: number;
  [key: string]: unknown;
}

interface HealthResponse {
  type?: string;
  event?: string;
  payload?: HealthPayload;
  [key: string]: unknown;
}

interface SkillEntry {
  name?: string;
  enabled?: boolean;
  [key: string]: unknown;
}

interface SkillStatus {
  skills?: SkillEntry[];
}

interface McpServersResponse {
  servers?: Record<string, unknown>;
}

interface ConfigResponse {
  owner_id: string;
  config: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function extractHealth(data: HealthResponse): HealthPayload {
  if (data.type === "event" && data.payload) {
    return data.payload;
  }
  return data as HealthPayload;
}

/** A count that arrives either as a number (Hermes) or as a list/`{count}` (OpenClaw).
 *  `undefined`/`null` stays unknown \u2014 the card renders an em dash rather than a 0. */
export function healthCount(
  value: HealthAgent[] | { count?: number } | number | null | undefined,
): number | undefined {
  if (typeof value === "number") return value;
  if (Array.isArray(value)) return value.length;
  if (value && typeof value === "object") return value.count;
  return undefined;
}

function formatUptimeSeconds(seconds: number | undefined): string {
  if (seconds == null || seconds < 0) return "\u2014";
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 24) return `${Math.floor(hours / 24)}d ${hours % 24}h`;
  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  return `${minutes}m`;
}

function formatUptime(ts: number | undefined): string {
  if (!ts) return "\u2014";
  const uptimeMs = Date.now() - ts;
  if (uptimeMs < 0) return "\u2014";
  const seconds = Math.floor(uptimeMs / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 24) return `${Math.floor(hours / 24)}d ${hours % 24}h`;
  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  return `${minutes}m`;
}

function formatTimeAgo(ts: number | string | undefined | null): string {
  if (!ts) return "\u2014";
  const ms = typeof ts === "string" ? new Date(ts).getTime() : ts;
  const ago = Date.now() - ms;
  if (ago < 0) return "\u2014";
  const minutes = Math.floor(ago / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface OverviewPanelProps {
  onPanelChange?: (panel: string) => void;
}

export function OverviewPanel({ onPanelChange }: OverviewPanelProps = {}) {
  const api = useApi();
  const statusLoaded = useStatusLoaded();
  const [configOpen, setConfigOpen] = useState(false);

  const {
    data: rawHealth,
    error: healthError,
    isLoading: healthLoading,
    mutate: refreshHealth,
  } = useRuntimeRpc<HealthResponse>(statusLoaded ? "health" : null, undefined, {
    refreshInterval: 10000,
  });

  const { container, isLoading: statusLoading, error: statusError } = useProvisioningState();
  const { account } = useBilling();
  const subscriptionStatus = account?.subscription_status ?? null;
  // Captured once (not read directly in render — Date.now() is impure) so
  // this is stable across re-renders. Docked next to WorkspaceSidebar's
  // footer badge in the same dockable shell (WorkspaceShell.tsx), so it must
  // agree on expired — a user with Overview open next to chat must not see
  // "Free tier" here while the sidebar footer says "Free tier ended".
  const [nowMs] = useState(() => Date.now());
  const isTrialExpired = isFreeTierExpired(subscriptionStatus, account?.trial_end, nowMs);

  // Skills count comes from the same RPC SkillsTab uses; pass no agentId
  // to get the global skills inventory. Response can be either an array or
  // {skills: [...]} (see SkillsTab.tsx). Held until the runtime is DEFINITE —
  // `undefined` is "not known", and guessing costs a microVM start on AgentCore.
  const { data: skillsRaw } = useRuntimeRpc<SkillStatus | SkillEntry[]>(
    statusLoaded ? "skills.status" : null,
  );

  // MCP server count via the integrations REST endpoint. The endpoint
  // gates org-scoped reads behind org_admin (entries carry `env` secrets
  // only the admin set up), so non-admin org members get 403 and we
  // surface "—" rather than fabricate a count.
  const { data: mcpData, error: mcpError } = useSWR<McpServersResponse>(
    "/integrations/mcp/servers",
    () => api.get("/integrations/mcp/servers") as Promise<McpServersResponse>,
    { shouldRetryOnError: false },
  );
  const mcpForbidden =
    (mcpError as { status?: number } | undefined)?.status === 403;

  // Lazy-load the raw config — only fetch when the dialog opens, so the
  // overview panel doesn't pay for a config read on every mount.
  // Non-admin org members get 403 from this endpoint (the config carries
  // shared secrets); we surface that as a "ask your admin" message rather
  // than the generic "no config on disk" fallback.
  const {
    data: configData,
    error: configError,
    isLoading: configLoading,
  } = useSWR<ConfigResponse>(
    configOpen ? "/config" : null,
    () => api.get("/config") as Promise<ConfigResponse>,
  );
  const configForbidden =
    (configError as { status?: number } | undefined)?.status === 403;

  const { state: healthState, reason: healthReason, canRecover, actionLabel, recover, isRecovering } = useSystemHealth();

  // `!statusLoaded` counts as still-loading: the health RPC is held until the
  // runtime is known, and without this the panel would fall through to
  // "No container available" in the gap before the first status body lands.
  const isLoading = (healthLoading || !statusLoaded) && statusLoading;
  const error = healthError || statusError;

  const handleRefresh = () => {
    refreshHealth();
  };

  if (isLoading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 size={20} className="animate-spin text-[var(--midnight-muted)]" />
      </div>
    );
  }

  if (error && !rawHealth && !container) {
    return (
      <div className="p-6 space-y-3">
        <p className="text-sm text-destructive">Failed to fetch status: {error.message}</p>
        <Button variant="outline" size="sm" onClick={handleRefresh}>
          <RefreshCw size={16} className="mr-1.5" /> Retry
        </Button>
      </div>
    );
  }

  if (!rawHealth && !container) {
    return (
      <div className="p-6 text-sm text-[var(--midnight-muted)]">No container available.</div>
    );
  }

  const health = rawHealth ? extractHealth(rawHealth) : ({} as HealthPayload);
  // ECS answers `{ok}`; the Hermes bridge answers `{ready}` and carries no `ok` at all — accept
  // either so a healthy AgentCore workspace doesn't render "Offline" (see the HealthPayload note).
  const isOnline = health.ok === true || health.ready === true;
  const agentCount = healthCount(health.agents);
  // Normalize the skills.status response shape — same trick SkillsTab uses.
  const skillsList: SkillEntry[] = Array.isArray(skillsRaw)
    ? skillsRaw
    : skillsRaw?.skills ?? [];
  const skillCount = skillsRaw === undefined ? undefined : skillsList.length;
  // Count is unknown for forbidden (non-admin org member) — render "—".
  const mcpCount = mcpForbidden
    ? undefined
    : mcpData === undefined
      ? undefined
      : Object.keys(mcpData.servers ?? {}).length;

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="font-editorial-serif text-title">Overview</h2>
          <p className="text-xs text-[var(--midnight-muted)]">
            Container status and gateway health snapshot.
          </p>
        </div>
        <div className="flex items-center gap-1">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setConfigOpen(true)}
                className="text-xs gap-1.5"
              >
                <FileText size={16} />
                Configuration
              </Button>
            </TooltipTrigger>
            <TooltipContent>View raw openclaw.json</TooltipContent>
          </Tooltip>
          <Button variant="ghost" size="sm" onClick={handleRefresh}>
            <RefreshCw size={16} />
          </Button>
        </div>
      </div>

      {/* Two-column: Container Info + Snapshot */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Container Info */}
        <div className="rounded-lg border border-[var(--edge)] bg-cream p-4 space-y-3">
          <h3 className="label-caps text-[var(--midnight-muted)]">
            Container Info
          </h3>
          <div className="space-y-2">
            <InfoRow
              icon={<Activity size={16} />}
              label="Status"
              value={container?.container_status ?? "\u2014"}
              badge={
                container?.container_status === "running"
                  ? "green"
                  : container?.container_status === "provisioning"
                    ? "yellow"
                    : "red"
              }
            />
            <InfoRow
              icon={<CreditCard size={16} />}
              label="Subscription"
              value={planLabel(subscriptionStatus, { expired: isTrialExpired }) ?? "—"}
            />
            <InfoRow
              icon={<MapPin size={16} />}
              label="Region"
              value={container?.region ?? "\u2014"}
            />
            <InfoRow
              icon={<Clock size={16} />}
              label="Created"
              value={formatTimeAgo(container?.created_at)}
            />
          </div>
        </div>

        {/* Snapshot */}
        <div className="rounded-lg border border-[var(--edge)] bg-cream p-4 space-y-3">
          <h3 className="label-caps text-[var(--midnight-muted)]">
            Snapshot
          </h3>
          <div className="space-y-2">
            <InfoRow
              icon={isOnline ? <Wifi size={16} /> : <WifiOff size={16} />}
              label="Status"
              value={isOnline ? "OK" : "Offline"}
              badge={isOnline ? "green" : "red"}
            />
            <InfoRow
              icon={<Clock size={16} />}
              label="Uptime"
              value={
                health.uptimeS != null
                  ? formatUptimeSeconds(health.uptimeS)
                  : formatUptime(health.ts)
              }
            />
          </div>
        </div>
      </div>

      {/* Summary Row \u2014 clickable cards deep-link to the relevant panel.
          Skills -> SkillsTab; MCP -> IntegrationsPanel
          (the raw MCP-servers escape hatch now lives under Integrations > Advanced). */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
        <SummaryCard
          icon={Users}
          label="Instances"
          value={agentCount !== undefined ? String(agentCount) : "\u2014"}
          onClick={onPanelChange ? () => onPanelChange("agents") : undefined}
        />
        <SummaryCard
          icon={Wrench}
          label="Skills"
          value={skillCount !== undefined ? String(skillCount) : "\u2014"}
          onClick={onPanelChange ? () => onPanelChange("skills") : undefined}
        />
        <SummaryCard
          icon={Server}
          label="MCP"
          value={mcpCount !== undefined ? String(mcpCount) : "\u2014"}
          onClick={onPanelChange ? () => onPanelChange("integrations") : undefined}
        />
      </div>

      {/* Health Summary */}
      <div className="rounded-lg border border-[var(--edge)] bg-[var(--cream-deep)] p-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span
              className={cn(
                "h-2.5 w-2.5 rounded-full",
                healthState === "HEALTHY" ? "bg-ok" :
                healthState === "STARTING" || healthState === "RECOVERING" || healthState === "GATEWAY_BUSY" ? "bg-warn animate-pulse" :
                "bg-err"
              )}
            />
            <span className="text-sm font-medium text-[var(--midnight)]">
              {healthState === "HEALTHY" ? "System Healthy" :
               healthState === "STARTING" ? "Starting..." :
               healthState === "RECOVERING" ? "Recovering..." :
               healthState === "GATEWAY_BUSY" ? "Agent Thinking" :
               "Container Down"}
            </span>
          </div>
          {canRecover && actionLabel && (
            <Button
              variant="outline"
              size="sm"
              onClick={recover}
              disabled={isRecovering}
              className="text-xs"
            >
              {isRecovering ? (
                <Loader2 size={12} strokeWidth={1.75} className="animate-spin mr-1" />
              ) : null}
              {actionLabel}
            </Button>
          )}
        </div>
        <p className="text-xs text-[var(--midnight-muted)] mt-1">{healthReason}</p>
      </div>

      {/* Raw openclaw.json viewer — read-only, syntax-highlighting kept
          minimal (browser monospace + JSON.stringify). For an editable
          surface, we'd add structured cards backed by PATCH /config. */}
      <Dialog open={configOpen} onOpenChange={setConfigOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>openclaw.json</DialogTitle>
            <DialogDescription>
              Read-only view of the configuration your container is running.
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-[60vh] overflow-auto rounded-md border border-[var(--edge)] bg-cream-deep p-3">
            {configLoading ? (
              <div className="flex items-center gap-2 text-sm text-[var(--midnight-muted)]">
                <Loader2 size={16} className="animate-spin" /> Loading…
              </div>
            ) : configForbidden ? (
              <p className="text-sm text-[var(--midnight-muted)]">
                Org admins only. The configuration carries shared secrets
                (channel tokens, MCP env) — ask your org admin to view it.
              </p>
            ) : configData?.config ? (
              <pre className="text-xs font-mono whitespace-pre-wrap break-words">
                {JSON.stringify(configData.config, null, 2)}
              </pre>
            ) : (
              <p className="text-sm text-[var(--midnight-muted)]">
                No config on disk yet. This is normal during provisioning.
              </p>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function InfoRow({
  icon,
  label,
  value,
  badge,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  badge?: "green" | "yellow" | "red";
}) {
  const badgeClass =
    badge === "green"
      ? "border border-ok text-ok bg-cream"
      : badge === "yellow"
        ? "border border-warn text-warn bg-cream"
        : badge === "red"
          ? "border border-err text-err bg-cream"
          : "";

  return (
    <div className="flex items-center justify-between text-sm">
      <div className="flex items-center gap-2 text-[var(--midnight-muted)]">
        {icon}
        <span>{label}</span>
      </div>
      {badge ? (
        <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${badgeClass}`}>
          {value}
        </span>
      ) : (
        <span className="font-medium truncate max-w-[180px]">{value}</span>
      )}
    </div>
  );
}

function SummaryCard({
  icon: Icon,
  label,
  value,
  onClick,
  subLabel,
  subLabelTone,
}: {
  icon: typeof Clock;
  label: string;
  value: string;
  onClick?: () => void;
  subLabel?: string;
  subLabelTone?: "red" | "amber";
}) {
  const interactive = !!onClick;
  const Tag = interactive ? "button" : "div";
  return (
    <Tag
      type={interactive ? "button" : undefined}
      onClick={onClick}
      className={cn(
        "rounded-lg border border-[var(--edge)] bg-cream p-3 text-center w-full",
        interactive &&
          "cursor-pointer hover:border-[var(--midnight-faint)] hover:bg-cream-deep transition-colors duration-(--dur-fast) ease-out",
      )}
    >
      <div className="flex items-center justify-center gap-1.5 mb-1">
        <Icon size={12} strokeWidth={1.75} className="text-midnight-label" />
        <span className="label-caps">
          {label}
        </span>
      </div>
      <div className="text-sm font-medium">{value}</div>
      {subLabel ? (
        <div
          className={cn(
            "text-2xs font-medium mt-0.5",
            subLabelTone === "red"
              ? "text-err"
              : subLabelTone === "amber"
                ? "text-warn"
                : "text-midnight-muted",
          )}
        >
          {subLabel}
        </div>
      ) : null}
    </Tag>
  );
}

