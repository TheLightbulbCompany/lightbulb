"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, AlertCircle } from "lucide-react";

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
import { GatewayProvider } from "@/hooks/useGateway";
import { useBilling } from "@/hooks/useBilling";
import { useChannelLinks } from "@/hooks/useChannelLinks";

export function MyChannelsSection() {
  // The bot setup wizard pokes the user's container gateway over WS (to
  // poll channels.status during the enable→token→pair dance). The settings
  // route isn't wrapped in a GatewayProvider like /chat is, so we mount a
  // scoped provider here. useGateway lazily opens its socket, so if the
  // user never opens the wizard there's no extra connection.
  return (
    <GatewayProvider>
      <MyChannelsSectionInner />
    </GatewayProvider>
  );
}

function MyChannelsSectionInner() {
  const api = useApi();
  const { isSubscribed } = useBilling();
  // bot_username is now populated by the backend via channels.status probe
  const { channelLinks: data, error, isLoading, refresh: mutate } = useChannelLinks(true);
  const [wizard, setWizard] = useState<{ provider: Provider; agentId: string } | null>(null);
  const [unlinkTarget, setUnlinkTarget] = useState<{ provider: Provider; agentId: string } | null>(null);
  const [unlinking, setUnlinking] = useState(false);

  useEffect(() => {
    if (!wizard && !unlinkTarget) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setWizard(null);
        setUnlinkTarget(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [wizard, unlinkTarget]);

  if (error) {
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

  const performUnlink = async () => {
    if (!unlinkTarget) return;
    setUnlinking(true);
    try {
      await api.del(`/channels/link/${unlinkTarget.provider}/${unlinkTarget.agentId}`);
      setUnlinkTarget(null);
      await mutate();
    } finally {
      setUnlinking(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold">My Channels</h2>
        <p className="text-xs text-[#8a8578]">
          Link your Telegram, Discord, and Slack identities to your organization&apos;s bots.
        </p>
      </div>

      {PROVIDERS.map((provider) => {
        const bots = data[provider];
        return (
          <div key={provider} className="space-y-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-[#8a8578]">
              {PROVIDER_LABELS[provider]}
            </h3>
            {bots.length === 0 ? (
              <div className="rounded-md border border-[#e0dbd0] p-3 text-xs text-[#8a8578]">
                {!isSubscribed ? (
                  <>Channels require an active subscription. Sign up to connect {PROVIDER_LABELS[provider]}.</>
                ) : (
                  <>
                    No {PROVIDER_LABELS[provider]} bots set up in this container.
                    {data.can_create_bots && " Set one up from your agent's Channels tab."}
                  </>
                )}
              </div>
            ) : (
              <div className="space-y-2">
                {bots.map((bot) => (
                  <div
                    key={bot.agent_id}
                    className="flex items-center gap-3 rounded-md border border-[#e0dbd0] p-3"
                  >
                    {bot.linked ? (
                      <CheckCircle2 className="h-4 w-4 text-[#2d8a4e]" />
                    ) : (
                      <AlertCircle className="h-4 w-4 text-amber-500" />
                    )}
                    <div className="flex-1">
                      <p className="text-sm font-mono">{formatBotHandle(provider, bot.bot_username)}</p>
                      <p className="text-xs text-[#8a8578]">{bot.agent_id}</p>
                    </div>
                    {bot.linked ? (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setUnlinkTarget({ provider, agentId: bot.agent_id })}
                        aria-label={`Unlink your ${PROVIDER_LABELS[provider]} from ${bot.bot_username}`}
                      >
                        Unlink
                      </Button>
                    ) : (
                      <Button
                        size="sm"
                        onClick={() => setWizard({ provider, agentId: bot.agent_id })}
                        aria-label={`Link your ${PROVIDER_LABELS[provider]} to ${bot.bot_username}`}
                      >
                        Link
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}

      {wizard && (
        <div
          className="fixed inset-0 bg-black/30 flex items-center justify-center z-50"
          role="dialog"
          aria-modal="true"
          onClick={() => setWizard(null)}
        >
          <div
            className="bg-white rounded-lg shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <BotSetupWizard
              mode="link-only"
              provider={wizard.provider}
              agentId={wizard.agentId}
              onComplete={() => {
                setWizard(null);
                mutate();
              }}
              onCancel={() => setWizard(null)}
            />
          </div>
        </div>
      )}

      <AlertDialog open={unlinkTarget !== null} onOpenChange={(open) => !open && setUnlinkTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Unlink your account?</AlertDialogTitle>
            <AlertDialogDescription>
              {unlinkTarget && (
                <>
                  This removes your {PROVIDER_LABELS[unlinkTarget.provider]} from the
                  <span className="font-mono"> {unlinkTarget.agentId} </span>
                  bot. You can re-link anytime by pasting a new pairing code.
                </>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={unlinking}>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={performUnlink} disabled={unlinking}>
              {unlinking ? "Unlinking…" : "Unlink"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
