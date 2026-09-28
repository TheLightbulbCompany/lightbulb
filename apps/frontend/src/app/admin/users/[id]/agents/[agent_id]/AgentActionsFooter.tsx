"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { Card } from "@/components/admin/Card";
import { ConfirmActionDialog } from "@/components/admin/ConfirmActionDialog";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { Button } from "@/components/ui/button";
import { deleteAgent } from "@/app/admin/_actions/agent";

export interface AgentActionsFooterProps {
  userId: string;
  agentId: string;
  agentName?: string;
}

/**
 * Client island for the destructive agent action. Lives next to the parent
 * SC so the page itself stays server-rendered. It is wrapped in
 * the typed-confirmation dialog (CEO S5) — the dialog enforces the typed
 * phrase and the 3-attempt lockout. We surface backend errors inline rather
 * than throwing so the SC stays mounted.
 */
export function AgentActionsFooter({
  userId,
  agentId,
  agentName,
}: AgentActionsFooterProps) {
  const router = useRouter();
  const [error, setError] = React.useState<string | null>(null);

  // Plain async handlers — React Compiler memoizes them and avoids the
  // "preserve manual memoization" linter friction useCallback would trigger.
  async function handleDelete() {
    setError(null);
    const result = await deleteAgent(userId, agentId);
    if (!result.ok) {
      setError(result.error ?? "delete_failed");
      return;
    }
    // Successful delete invalidates the detail route — bounce back to the list.
    router.push(`/admin/users/${encodeURIComponent(userId)}/agents`);
    router.refresh();
  }

  return (
    <Card title="Destructive actions">
      <div className="space-y-3">
        {error ? <ErrorBanner error={error} variant="error" /> : null}
        <div className="flex flex-wrap gap-2">
          <ConfirmActionDialog
            confirmText={agentId}
            actionLabel="Delete agent"
            destructive
            onConfirm={handleDelete}
          >
            <Button type="button" variant="destructive" size="sm">
              Delete agent
            </Button>
          </ConfirmActionDialog>

        </div>
      </div>
    </Card>
  );
}
