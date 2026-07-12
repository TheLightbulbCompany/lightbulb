"use client";

import { useEffect, useState } from "react";
import { Plus, Trash2, CheckCircle2, AlertCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useApi } from "@/lib/api";
import { type Provider, PROVIDERS, PROVIDER_LABELS, formatBotHandle } from "@/lib/channels";
import { BotSetupWizard } from "@/components/channels/BotSetupWizard";
import { useBilling } from "@/hooks/useBilling";
import { useChannelLinks } from "@/hooks/useChannelLinks";

interface AgentChannelsSectionProps {
  agentId: string;
}

export function AgentChannelsSection({ agentId }: AgentChannelsSectionProps) {
  const api = useApi();
  const { isSubscribed } = useBilling();
  const { channelLinks: data, error, isLoading, refresh: mutate } = useChannelLinks(true);
  const [wizardFor, setWizardFor] = useState<{ provider: Provider; mode: "create" | "link-only" } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Provider | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (!wizardFor && !deleteTarget) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setWizardFor(null);
        setDeleteTarget(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [wizardFor, deleteTarget]);

  // Error screen only when there's nothing to show: SWR keeps stale `data`
  // through a failed revalidation, and a transient blip (a mutate() after
  // wizard completion hitting one 5xx) must not swap an already-rendered bot
  // list for an error card that never self-heals.
  if (error && !data) {
    return (
      <div className="p-4 space-y-2">
        <p className="text-sm text-[#dc2626]">Failed to load channels.</p>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          Retry
        </Button>
      </div>
    );
  }

  if (isLoading || !data) {
    return <div className="p-4 text-sm text-[#8a8578]">Loading channels…</div>;
  }

  const performDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api.del(`/channels/${deleteTarget}/${agentId}`);
      setDeleteTarget(null);
      await mutate();
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="space-y-4 p-4">
      <h3 className="text-sm font-semibold">Channels</h3>
      {PROVIDERS.map((provider) => {
        const bots = data[provider].filter((b) => b.agent_id === agentId);
        return (
          <div key={provider} className="rounded-md border border-[#e0dbd0] p-3">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-[#8a8578]">
                {PROVIDER_LABELS[provider]}
              </span>
            </div>
            {!isSubscribed ? (
              <p className="text-xs text-[#8a8578]">
                Channels require an active subscription. Sign up to connect {PROVIDER_LABELS[provider]}.
              </p>
            ) : bots.length === 0 ? (
              <p className="text-xs text-[#8a8578]">No bot configured</p>
            ) : (
              bots.map((bot) => (
                <div key={bot.agent_id} className="flex items-center gap-2 text-sm">
                  {bot.linked ? (
                    <CheckCircle2 className="h-4 w-4 text-[#2d8a4e]" />
                  ) : (
                    <AlertCircle className="h-4 w-4 text-amber-500" />
                  )}
                  <span className="font-mono">{formatBotHandle(provider, bot.bot_username)}</span>
                  <div className="flex-1" />
                  {!bot.linked && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setWizardFor({ provider, mode: "link-only" })}
                      aria-label={`Link your ${PROVIDER_LABELS[provider]} to ${bot.bot_username}`}
                    >
                      Link
                    </Button>
                  )}
                  {data.can_create_bots && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setDeleteTarget(provider)}
                      aria-label={`Delete ${provider} bot`}
                    >
                      <Trash2 className="h-3 w-3" />
                    </Button>
                  )}
                </div>
              ))
            )}
            {isSubscribed && data.can_create_bots && bots.length === 0 && (
              <Button
                variant="outline"
                size="sm"
                className="mt-2"
                onClick={() => setWizardFor({ provider, mode: "create" })}
              >
                <Plus className="h-3 w-3 mr-1" />
                Add {PROVIDER_LABELS[provider]} bot
              </Button>
            )}
          </div>
        );
      })}

      {wizardFor && (
        <div
          className="fixed inset-0 bg-black/30 flex items-center justify-center z-50"
          role="dialog"
          aria-modal="true"
          onClick={() => setWizardFor(null)}
        >
          <div
            className="bg-white rounded-lg shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <BotSetupWizard
              mode={wizardFor.mode}
              provider={wizardFor.provider}
              agentId={agentId}
              onComplete={() => {
                setWizardFor(null);
                mutate();
              }}
              onCancel={() => setWizardFor(null)}
            />
          </div>
        </div>
      )}

      <AlertDialog open={deleteTarget !== null} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this bot?</AlertDialogTitle>
            <AlertDialogDescription>
              {deleteTarget && (
                <>
                  This removes the {PROVIDER_LABELS[deleteTarget]} bot from the
                  <span className="font-mono"> {agentId} </span>
                  agent and unlinks every member who paired with it. This cannot be undone.
                </>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={performDelete} disabled={deleting}>
              {deleting ? "Deleting…" : "Delete bot"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
