"use client";

import { useState, useRef } from "react";
import { Loader2 } from "lucide-react";

import { capture } from "@/lib/analytics";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import type { Agent } from "@/hooks/useAgents";
import { normalizeAgentId, validateAgentName } from "@/lib/agentIdentity";

// =============================================================================
// Notes on dialog state and lifecycle
// =============================================================================
//
// Each dialog is meant to be remounted by the parent (via a `key` prop tied
// to its open/target state) so its internal state initializes fresh on each
// open. That avoids the "reset state on close" useEffect anti-pattern that
// `react-hooks/set-state-in-effect` flags.
//
// The action button is a plain `Button`, NOT `AlertDialogAction`. Radix's
// `AlertDialogAction` auto-closes the dialog when clicked, which races our
// `submitting` state — by the time the close fires `onOpenChange`, the
// `setSubmitting(true)` queued in `handleSubmit` hasn't committed yet, so
// the close handler sees stale `submitting === false` and unmounts the
// dialog before the RPC resolves. On failure that hides the error. With
// `Button`, we control the close ourselves: the parent's onSuccess closes
// it after the awaited mutation succeeds; on error the dialog stays open
// and the inline error renders.
//
// The same race exists in `onOpenChange` if the user clicks outside the
// dialog mid-submit. We guard against it with a ref (`submittingRef`) so
// the close-attempt sees the live value, not the stale React state.

// =============================================================================
// Create
// =============================================================================

interface CreateProps {
  open: boolean;
  existingIds: string[];
  onCancel: () => void;
  onCreate: (name: string) => Promise<void>;
}

export function AgentCreateDialog({ open, existingIds, onCancel, onCreate }: CreateProps) {
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);

  const id = normalizeAgentId(name);
  const clientError = validateAgentName(name, existingIds);
  const canSubmit = name.trim() !== "" && clientError === null && !submitting;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    submittingRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      await onCreate(name.trim());
      // On success the parent closes the dialog by clearing the open prop.
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  return (
    <AlertDialog open={open} onOpenChange={(next) => !next && !submittingRef.current && onCancel()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>New agent</AlertDialogTitle>
          <AlertDialogDescription>
            Give your agent a name. Its workspace, tools, and memory are isolated from your other agents.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div className="space-y-2">
          <input
            type="text"
            autoFocus
            value={name}
            onChange={(e) => { setName(e.target.value); setError(null); }}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleSubmit();
              if (e.key === "Escape" && !submitting) onCancel();
            }}
            placeholder="e.g. Research Assistant"
            disabled={submitting}
            className="w-full rounded-md border border-[#e0dbd0] bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#2d8a4e]/30 disabled:opacity-50"
          />
          {id && !clientError && (
            <p className="text-[11px] text-[#8a8578]">ID: {id}</p>
          )}
          {(clientError || error) && (
            <p className="text-xs text-red-500">{clientError || error}</p>
          )}
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={submitting}>Cancel</AlertDialogCancel>
          <Button onClick={handleSubmit} disabled={!canSubmit}>
            {submitting ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : null}
            Create
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

// =============================================================================
// Rename
// =============================================================================

interface RenameProps {
  agent: Agent | null;
  onCancel: () => void;
  onRename: (name: string) => Promise<void>;
}

export function AgentRenameDialog({ agent, onCancel, onRename }: RenameProps) {
  const [name, setName] = useState(agent?.identity?.name || agent?.name || agent?.id || "");
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = name.trim() !== "" && !submitting;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    submittingRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      await onRename(name.trim());
      capture("agent_renamed", { agent_id: agent?.id });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  return (
    <AlertDialog open={agent !== null} onOpenChange={(next) => !next && !submittingRef.current && onCancel()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Rename agent</AlertDialogTitle>
          <AlertDialogDescription>
            The agent&apos;s ID stays the same — only the display name changes.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div className="space-y-2">
          <input
            type="text"
            autoFocus
            value={name}
            onChange={(e) => { setName(e.target.value); setError(null); }}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleSubmit();
              if (e.key === "Escape" && !submitting) onCancel();
            }}
            disabled={submitting}
            className="w-full rounded-md border border-[#e0dbd0] bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#2d8a4e]/30 disabled:opacity-50"
          />
          {error && <p className="text-xs text-red-500">{error}</p>}
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={submitting}>Cancel</AlertDialogCancel>
          <Button onClick={handleSubmit} disabled={!canSubmit}>
            {submitting ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : null}
            Rename
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

// =============================================================================
// Delete
// =============================================================================

interface DeleteProps {
  agent: Agent | null;
  onCancel: () => void;
  onDelete: () => Promise<void>;
}

export function AgentDeleteDialog({ agent, onCancel, onDelete }: DeleteProps) {
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    submittingRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      await onDelete();
      capture("agent_deleted", { agent_id: agent?.id });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  const displayName = agent?.identity?.name || agent?.name || agent?.id || "";

  return (
    <AlertDialog open={agent !== null} onOpenChange={(next) => !next && !submittingRef.current && onCancel()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete {displayName}?</AlertDialogTitle>
          <AlertDialogDescription>
            This removes the agent&apos;s workspace, files, and session history. This cannot be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error && <p className="text-xs text-red-500">{error}</p>}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={submitting}>Cancel</AlertDialogCancel>
          <Button variant="destructive" onClick={handleSubmit} disabled={submitting}>
            {submitting ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : null}
            Delete
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
