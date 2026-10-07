"use client";

import { useState } from "react";
import { Check, Copy, ExternalLink } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useApi } from "@/lib/api";
import { ApiError, type Connection } from "@/hooks/useConnections";

interface AwsConnectCardProps {
  connection: Connection;
  /** Whether the current user may manage connections (org admin). */
  canManage: boolean;
  /**
   * Re-fetch the connections list (the `useConnections` SWR `mutate`). Called
   * after `aws-complete` succeeds so the row flips to `connected` and this
   * card unmounts in favour of the standard Disconnect affordance.
   */
  refreshConnections?: () => Promise<unknown>;
}

/** One scope's managed-policy ARN + its CloudFormation quick-create link. */
interface AwsScopeOption {
  policy_arn: string;
  /** Null until the connector CFN template is hosted (backend Plan 3). */
  quick_create_url: string | null;
}

/** Shape returned by `GET /connections/aws/aws-setup` (connections.py). */
interface AwsSetupResponse {
  external_id: string;
  principal_arn: string;
  our_account_id: string;
  default_region: string;
  view_only: AwsScopeOption;
  read_only: AwsScopeOption;
  power_user: AwsScopeOption;
}

type AwsScope = "view_only" | "read_only" | "power_user";

const NON_ADMIN_MSG = "Only organization admins can manage connections.";

/**
 * A concrete IAM role ARN with a 12-digit account. Mirrors the backend's
 * `_ROLE_ARN_RE` (connections.py) — wildcards in the role-path are excluded —
 * so the Connect button only enables for an ARN the backend will accept.
 */
const ROLE_ARN_RE = /^arn:aws:iam::\d{12}:role\/[\w+=,.@/-]+$/;

/**
 * Maps a connect/setup ApiError to a short, user-facing string. The backend
 * hands back a useful `detail` (→ ApiError.message) for the 400/409 cases —
 * "role must be in your own AWS account, not Isol8's", "run aws-setup first",
 * "invalid role ARN" — so those surface verbatim. 403 is special-cased for the
 * non-admin permission state.
 */
function awsErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return NON_ADMIN_MSG;
    if (e.message && e.message !== `API ${e.status}`) return e.message;
    if (e.status === 409) return "Run setup first, then paste the role ARN.";
    if (e.status === 400) return "That role ARN was rejected.";
    if (e.status === 502) return "Could not apply the connection. Try again.";
  }
  return e instanceof Error ? e.message : "Something went wrong.";
}

/**
 * AWS read-only connect card (acquisition `aws_sts`). AWS does NOT take a
 * pasted token — it uses an STS assume-role flow:
 *   1. choose a scope (ViewOnly default, or the broader ReadOnly);
 *   2. `aws-setup` mints the per-owner ExternalId + returns our principal ARN
 *      and the scope's CloudFormation quick-create link;
 *   3. the owner creates a cross-account role in their own AWS account (trusting
 *      our principal ARN + ExternalId) and pastes its ARN back;
 *   4. `aws-complete { role_arn, region }` grants + materializes + connects.
 *
 * Progressive disclosure mirrors ConnectButton's BYO panel: a collapsed Connect
 * button expands into the inline card.
 */
export function AwsConnectCard({ connection, canManage, refreshConnections }: AwsConnectCardProps) {
  const { provider, display_name } = connection;
  const api = useApi();

  const [open, setOpen] = useState(false);
  const [scope, setScope] = useState<AwsScope>("view_only");
  const [setup, setSetup] = useState<AwsSetupResponse | null>(null);
  const [loadingSetup, setLoadingSetup] = useState(false);
  const [roleArn, setRoleArn] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const loadSetup = async () => {
    setLoadingSetup(true);
    setErr(null);
    try {
      const r = (await api.get(
        `/connections/${encodeURIComponent(provider)}/aws-setup`,
      )) as AwsSetupResponse;
      setSetup(r);
    } catch (e) {
      setErr(awsErrorMessage(e));
    } finally {
      setLoadingSetup(false);
    }
  };

  const openCard = () => {
    setOpen(true);
    setErr(null);
    // Stage the ExternalId + principal ARN on expand (idempotent server-side:
    // a repeat setup reuses the same ExternalId already baked into the trust
    // policy). Skip if we already have it from a prior expand this session.
    if (!setup) {
      void loadSetup();
    }
  };

  const closeCard = () => {
    setOpen(false);
    setRoleArn("");
    setErr(null);
  };

  const copy = async (key: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(key);
      window.setTimeout(() => setCopied((c) => (c === key ? null : c)), 1500);
    } catch {
      // Clipboard may be unavailable (insecure context / denied) — the value is
      // still selectable in the field, so a copy failure is a no-op.
    }
  };

  const roleArnValid = ROLE_ARN_RE.test(roleArn.trim());

  const connect = async () => {
    const arn = roleArn.trim();
    if (!roleArnValid || busy || !canManage || !setup) return;
    setBusy(true);
    setErr(null);
    try {
      // region is the setup default (us-east-1); the backend validates + writes
      // it into ~/.aws/config. On success the parent SWR `mutate` flips the row
      // to connected and this card unmounts for the standard Disconnect.
      await api.post(`/connections/${encodeURIComponent(provider)}/aws-complete`, {
        role_arn: arn,
        region: setup.default_region,
      });
      if (refreshConnections) {
        await refreshConnections();
      }
    } catch (e) {
      setErr(awsErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <div className="flex flex-col items-end gap-1">
        <Button
          size="sm"
          disabled={!canManage}
          onClick={openCard}
          aria-label={`Connect ${display_name}`}
        >
          Connect
        </Button>
      </div>
    );
  }

  const scopeOption = setup ? setup[scope] : null;
  const quickCreateUrl = scopeOption?.quick_create_url ?? null;

  const renderCopyableField = (id: string, label: string, value: string) => (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-[var(--midnight-muted)]">{label}</span>
      <div className="flex items-center gap-1">
        <code className="flex-1 break-all rounded-sm bg-cream px-2 py-1 text-caption text-midnight border border-[var(--edge)]">
          {value}
        </code>
        <Button
          size="sm"
          variant="outline"
          className="h-7 px-2"
          onClick={() => void copy(id, value)}
          aria-label={`Copy ${label}`}
        >
          {copied === id ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
        </Button>
      </div>
    </div>
  );

  return (
    <div className="flex w-80 flex-col items-stretch gap-2 rounded-md border border-[var(--edge)] bg-cream p-3">
      <p className="text-sm font-medium text-midnight">Connect {display_name} (read-only)</p>
      <p className="text-xs text-[var(--midnight-muted)]">
        Lightbulb will assume a role you create in your AWS account — no keys to paste.
      </p>

      {/* Step 1 — scope choice (ViewOnly default). */}
      <div className="flex flex-col gap-1">
        <span className="text-xs text-[var(--midnight-muted)]">Access scope</span>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant={scope === "view_only" ? "default" : "outline"}
            disabled={!canManage}
            onClick={() => setScope("view_only")}
            aria-pressed={scope === "view_only"}
          >
            ViewOnly
          </Button>
          <Button
            size="sm"
            variant={scope === "read_only" ? "default" : "outline"}
            disabled={!canManage}
            onClick={() => setScope("read_only")}
            aria-pressed={scope === "read_only"}
          >
            ReadOnly
          </Button>
          <Button
            size="sm"
            variant={scope === "power_user" ? "default" : "outline"}
            disabled={!canManage}
            onClick={() => setScope("power_user")}
            aria-pressed={scope === "power_user"}
          >
            PowerUser
          </Button>
        </div>
        {scope === "read_only" && (
          <span className="text-caption text-[var(--midnight-muted)]">broader — includes data-plane reads</span>
        )}
        {scope === "power_user" && (
          <span className="text-caption text-[var(--midnight-muted)]">read + write; excludes IAM / Organizations</span>
        )}
      </div>

      {/* Which AWS-managed policy to attach to the hand-created role. Only shown
          on the manual path: once the CFN quick-create link exists, the stack
          attaches the policy automatically and surfacing the ARN is noise. */}
      {setup && scopeOption && quickCreateUrl === null &&
        renderCopyableField("policy_arn", "Managed policy to attach", scopeOption.policy_arn)}

      {loadingSetup && <p className="text-xs text-[var(--midnight-muted)]">Loading setup…</p>}

      {setup && (
        <>
          {/* Step 2 — the trust-policy inputs the owner bakes into their role.
              Tucked behind a disclosure: the quick-create link below embeds
              both values automatically, so the normal flow never needs them
              surfaced. They're only relevant for manual (e.g. Terraform) role
              creation. */}
          <details className="group">
            <summary className="cursor-pointer text-xs text-[var(--midnight-muted)] hover:text-midnight">
              Advanced / manual setup
            </summary>
            <div className="mt-2 flex flex-col gap-2">
              <span className="text-caption text-[var(--midnight-muted)]">
                Only needed if you deploy the role manually (e.g. via Terraform) instead of the
                one-click button below.
              </span>
              {renderCopyableField("principal_arn", "Lightbulb principal ARN", setup.principal_arn)}
              {renderCopyableField("external_id", "ExternalId", setup.external_id)}
            </div>
          </details>

          {/* Create-role link for the chosen scope. Disabled when the CFN
              template isn't hosted yet — the role can still be created by hand
              and its ARN pasted below, so the flow keeps working. */}
          {quickCreateUrl ? (
            <Button
              size="sm"
              variant="outline"
              disabled={!canManage}
              onClick={() => window.open(quickCreateUrl, "_blank", "noopener,noreferrer")}
              aria-label="Create role in AWS"
            >
              <ExternalLink className="mr-1.5 h-3 w-3" />
              Create role in AWS
            </Button>
          ) : (
            <div className="flex flex-col gap-1">
              <Button size="sm" variant="outline" disabled aria-label="Create role in AWS">
                <ExternalLink className="mr-1.5 h-3 w-3" />
                Create role in AWS
              </Button>
              <span className="text-caption text-[var(--midnight-muted)]">
                AWS role template coming soon — paste an existing read-only role ARN below.
              </span>
            </div>
          )}

          {/* Step 3 — paste the created role's ARN and connect. */}
          <label htmlFor={`aws-role-arn-${provider}`} className="text-xs text-[var(--midnight-muted)]">
            Role ARN
          </label>
          <Input
            id={`aws-role-arn-${provider}`}
            type="text"
            autoComplete="off"
            spellCheck={false}
            value={roleArn}
            disabled={busy || !canManage}
            placeholder="arn:aws:iam::123456789012:role/IsolReadOnly"
            onChange={(e) => setRoleArn(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void connect();
              }
            }}
            className="h-9 bg-cream"
          />
        </>
      )}

      <div className="flex items-center justify-end gap-2">
        <Button size="sm" variant="outline" disabled={busy} onClick={closeCard}>
          Cancel
        </Button>
        {/* Setup failed (no staged ExternalId/principal). Connect is dead-ended
            since there's no setup to complete — offer an in-card retry so the
            user recovers without Cancel + reopen. */}
        {err && !setup && !loadingSetup ? (
          <Button
            size="sm"
            disabled={!canManage}
            onClick={() => void loadSetup()}
            aria-label="Retry setup"
          >
            Retry
          </Button>
        ) : (
          <Button
            size="sm"
            disabled={busy || !canManage || !setup || !roleArnValid}
            onClick={() => void connect()}
            aria-label={`Connect ${display_name}`}
          >
            {busy ? "Connecting…" : "Connect"}
          </Button>
        )}
      </div>

      {err && (
        <p className="text-xs text-[var(--err)]" role="alert">
          {err}
        </p>
      )}
    </div>
  );
}
