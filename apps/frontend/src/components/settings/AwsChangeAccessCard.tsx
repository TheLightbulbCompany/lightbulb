"use client";

import { useState } from "react";
import { Check, Copy, ExternalLink } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { type Connection } from "@/hooks/useConnections";
import {
  awsErrorMessage,
  type AwsScope,
  type AwsSetupResponse,
} from "@/components/settings/AwsConnectCard";

interface AwsChangeAccessCardProps {
  connection: Connection;
  /** Whether the current user may manage connections (org admin). */
  canManage: boolean;
}

/**
 * The fixed CloudFormation stack name the connector role lives in — mirrors
 * the backend's `bedrock_aip.CONNECTOR_ROLE_NAME` (`aws_cfn.py`'s
 * `_STACK_NAME`, which the quick-create link also pins). Not returned by
 * `aws-setup` since it never varies per owner.
 */
const STACK_NAME = "lightbulb-connector";

const SCOPE_LABELS: Record<AwsScope, string> = {
  view_only: "ViewOnly",
  read_only: "ReadOnly",
  power_user: "PowerUser",
};

/**
 * Region-scoped deep link to the CloudFormation *stack-update* console for the
 * existing connector stack. Region falls back to the setup default — the
 * Connection object stores no region today.
 */
function stackUpdateUrl(region: string): string {
  return `https://${region}.console.aws.amazon.com/cloudformation/home?region=${region}#/stacks/update/template?stackId=${STACK_NAME}`;
}

/**
 * Connected-state affordance to move an AWS connection between access tiers
 * WITHOUT reconnecting. The tier is NOT stored on Lightbulb's side — the
 * backend keys on the fixed `lightbulb-connector` role ARN and vends whatever
 * managed policy is attached — so switching is purely: update the CFN stack's
 * `ManagedPolicyArn` parameter in place; the next credential vend (≤1h) picks
 * up the new tier. This card does NO backend mutation — it reads aws-setup only
 * to surface the target tier's managed-policy ARN + region, and never touches
 * the disconnect / credential-vend paths. Disconnecting instead would require
 * deleting the stack first, so we deliberately steer the user here.
 */
export function AwsChangeAccessCard({ connection, canManage }: AwsChangeAccessCardProps) {
  const { provider } = connection;
  const api = useApi();

  const [open, setOpen] = useState(false);
  const [scope, setScope] = useState<AwsScope>("view_only");
  const [setup, setSetup] = useState<AwsSetupResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const loadSetup = async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = (await api.get(
        `/connections/${encodeURIComponent(provider)}/aws-setup`,
      )) as AwsSetupResponse;
      setSetup(r);
    } catch (e) {
      setErr(awsErrorMessage(e));
    } finally {
      setLoading(false);
    }
  };

  const openCard = () => {
    setOpen(true);
    setErr(null);
    if (!setup) void loadSetup();
  };

  const policyArn = setup ? setup[scope].policy_arn : null;
  const region = setup?.default_region ?? "us-east-1";

  const copyPolicy = async () => {
    if (!policyArn) return;
    try {
      await navigator.clipboard.writeText(policyArn);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard may be unavailable (insecure context / denied) — the ARN is
      // still selectable in the field, so a copy failure is a no-op.
    }
  };

  if (!open) {
    return (
      <Button
        size="sm"
        variant="outline"
        disabled={!canManage}
        onClick={openCard}
        aria-label="Change AWS access level"
      >
        Change access level
      </Button>
    );
  }

  return (
    <div className="flex w-80 flex-col items-stretch gap-2 rounded-md border border-[var(--edge)] bg-cream p-3">
      <p className="text-sm font-medium text-midnight">Change access level</p>
      <p className="text-xs text-[var(--midnight-muted)]">
        Update the access tier in place — no reconnect, no downtime. The new tier takes
        effect within about an hour.
      </p>

      {/* Target tier. We can't show the CURRENT tier — Lightbulb doesn't store
          it (the backend uses whatever managed policy the role carries). */}
      <div className="flex flex-col gap-1">
        <span className="text-xs text-[var(--midnight-muted)]">Access scope</span>
        <div className="flex gap-2">
          {(Object.keys(SCOPE_LABELS) as AwsScope[]).map((s) => (
            <Button
              key={s}
              size="sm"
              variant={scope === s ? "default" : "outline"}
              disabled={!canManage}
              onClick={() => setScope(s)}
              aria-pressed={scope === s}
            >
              {SCOPE_LABELS[s]}
            </Button>
          ))}
        </div>
        {scope === "read_only" && (
          <span className="text-caption text-[var(--midnight-muted)]">
            broader — includes data-plane reads
          </span>
        )}
        {scope === "power_user" && (
          <span className="text-caption text-[var(--midnight-muted)]">
            read + write; excludes IAM / Organizations
          </span>
        )}
      </div>

      {loading && <p className="text-xs text-[var(--midnight-muted)]">Loading…</p>}

      {setup && policyArn && (
        <>
          <div className="flex flex-col gap-1">
            <span className="text-xs text-[var(--midnight-muted)]">Managed policy for this tier</span>
            <div className="flex items-center gap-1">
              <code className="flex-1 break-all rounded-sm bg-cream px-2 py-1 text-caption text-midnight border border-[var(--edge)]">
                {policyArn}
              </code>
              <Button
                size="sm"
                variant="outline"
                className="h-7 px-2"
                onClick={() => void copyPolicy()}
                aria-label="Copy managed policy ARN"
              >
                {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
              </Button>
            </div>
          </div>

          <p className="text-caption text-[var(--midnight-muted)]">
            Set the <code className="text-midnight">ManagedPolicyArn</code> parameter of your{" "}
            <code className="text-midnight">{STACK_NAME}</code> CloudFormation stack to this
            value, then update the stack. The role ARN stays the same, so nothing reconnects.
          </p>

          <Button
            size="sm"
            variant="outline"
            disabled={!canManage}
            onClick={() => window.open(stackUpdateUrl(region), "_blank", "noopener,noreferrer")}
            aria-label="Update stack in AWS"
          >
            <ExternalLink className="mr-1.5 h-3.5 w-3.5" />
            Update stack in AWS
          </Button>
        </>
      )}

      <div className="flex items-center justify-end gap-2">
        <Button size="sm" variant="outline" onClick={() => setOpen(false)}>
          Close
        </Button>
        {/* Setup read failed (no policy ARNs to show). Offer an in-card retry. */}
        {err && !setup && !loading && (
          <Button
            size="sm"
            disabled={!canManage}
            onClick={() => void loadSetup()}
            aria-label="Retry loading access levels"
          >
            Retry
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
