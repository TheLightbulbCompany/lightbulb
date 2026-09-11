"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { Card } from "@/components/admin/Card";
import { ConfirmActionDialog } from "@/components/admin/ConfirmActionDialog";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { Button } from "@/components/ui/button";
import { retryProvision } from "@/app/admin/_actions/provisioning";

export interface ProvisioningActionsPanelProps {
  userId: string;
  /**
   * `container.status`; `null` for both "no row" and the container-read
   * error sentinel. Not currently read by this component — kept in the
   * props contract per the plan (callers already compute it for the page's
   * own "Container row" card) so it's available if a future lever needs it,
   * without widening the signature again.
   */
  containerStatus: string | null;
}

/**
 * Client island for the per-user provisioning levers (admin dashboard).
 * Structured like `ContainerActionsPanel.tsx`: section blocks with a title,
 * explanatory copy, a typed-confirm button (user_id as the confirm phrase,
 * per CEO S5), and an inline result/error banner.
 */
export function ProvisioningActionsPanel({
  userId,
}: ProvisioningActionsPanelProps) {
  const router = useRouter();
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);

  function clearStatus() {
    setError(null);
    setNotice(null);
  }

  function applyResult(label: string, ok: boolean, errMsg?: string) {
    if (ok) {
      setNotice(`${label} succeeded.`);
      router.refresh();
    } else {
      setError(`${label} failed: ${errMsg ?? "unknown_error"}`);
    }
  }

  // Plain async functions — React Compiler memoizes them (see
  // ContainerActionsPanel for why this beats manual useCallback here).
  async function handleRetry() {
    clearStatus();
    const result = await retryProvision(userId);
    applyResult("Retry provision", result.ok, result.error);
  }

  return (
    <Card title="Provisioning actions">
      <div className="space-y-3">
        {error ? <ErrorBanner error={error} variant="error" /> : null}
        {notice ? <ErrorBanner error={notice} variant="info" /> : null}

        <div className="space-y-2 rounded-lg border border-border p-3">
          <h3 className="text-xs font-medium uppercase tracking-caps text-muted-foreground">
            Retry provision
          </h3>
          <p className="text-xs text-muted-foreground">
            Re-dispatches provisioning through the Step Functions executor.
            Safe for error rows and for &apos;provisioning&apos; rows with a
            stale claim (wedge rescue); refuses (409) while a fresh attempt
            is in flight.
          </p>
          <ConfirmActionDialog
            confirmText={userId}
            actionLabel="Retry provision"
            onConfirm={handleRetry}
          >
            <Button type="button" variant="default" size="sm">
              Retry provision
            </Button>
          </ConfirmActionDialog>
        </div>
      </div>
    </Card>
  );
}
