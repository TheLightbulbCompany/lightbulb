"use client";

import { useRef, useState } from "react";

import { capture } from "@/lib/analytics";
import {
  AlertCircle,
  ArrowLeft,
  Check,
  Copy,
  ExternalLink,
  Loader2,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { awaitChannelReady } from "@/lib/awaitChannelReady";

type Provider = "telegram" | "discord" | "slack";
type Mode = "create" | "link-only";

export interface BotSetupWizardProps {
  mode: Mode;
  provider: Provider;
  agentId: string;
  /**
   * Optional bot handle to surface on the pair step. Required for the
   * link-only flow because members didn't create the bot themselves and
   * have no other way to know which bot to DM. Ignored when absent — the
   * pair step falls back to generic instructions.
   */
  botUsername?: string;
  onComplete: (result: { peer_id: string }) => void;
  onCancel: () => void;
}

// =============================================================================
// Provider metadata: human-readable labels, external links, copy, validation.
// Centralized so copy can be iterated without touching render logic.
// =============================================================================

const PROVIDER_LABELS: Record<Provider, string> = {
  telegram: "Telegram",
  discord: "Discord",
  slack: "Slack",
};

const PROVIDER_CREATE_URL: Record<Provider, string> = {
  telegram: "https://t.me/BotFather",
  discord: "https://discord.com/developers/applications",
  slack: "https://api.slack.com/apps?new_app=1",
};

// Slack manifest — dropped into the "From an app manifest" flow on api.slack.com.
const SLACK_APP_MANIFEST = `display_information:
  name: Isol8 Agent
features:
  bot_user:
    display_name: Isol8 Agent
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - im:history
      - im:read
      - im:write
      - users:read
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.im
  socket_mode_enabled: true`;

// Client-side format validation for the primary bot token. We're not doing
// cryptographic checks, just shape-matching so the user can't submit a token
// that obviously can't work (typos, pasting a webhook URL, etc). Real
// validation happens when OpenClaw tries to use it.
function validateToken(provider: Provider, token: string): string | null {
  const t = token.trim();
  if (!t) return "Token is required.";
  if (provider === "telegram") {
    if (!/^\d+:[A-Za-z0-9_-]{30,}$/.test(t)) {
      return "Expected format: 123456789:ABC-DEF...";
    }
  }
  if (provider === "slack") {
    if (!t.startsWith("xoxb-")) {
      return "Expected a Bot Token starting with xoxb-";
    }
  }
  // Discord tokens don't have a strictly enforced public format beyond
  // containing dots — skip client validation and let OpenClaw reject it.
  return null;
}

function validateSlackAppToken(token: string): string | null {
  const t = token.trim();
  if (!t) return "App-Level Token is required.";
  if (!t.startsWith("xapp-")) {
    return "Expected an App-Level Token starting with xapp-";
  }
  return null;
}

// =============================================================================
// Small presentational helpers
// =============================================================================

function StepIndicator({ current, total }: { current: number; total: number }) {
  return (
    <div className="flex items-center gap-1.5" aria-label={`Step ${current + 1} of ${total}`}>
      {Array.from({ length: total }).map((_, i) => (
        <span
          key={i}
          className={
            "h-1.5 rounded-full transition-all " +
            (i < current
              ? "w-4 bg-[#2d8a4e]"
              : i === current
                ? "w-6 bg-[#1a1a1a]"
                : "w-4 bg-[#e0dbd0]")
          }
        />
      ))}
    </div>
  );
}

function ExternalLinkButton({
  href,
  children,
}: {
  href: string;
  children: React.ReactNode;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1.5 rounded-md border border-[#e0dbd0] bg-white px-3 py-1.5 text-xs font-medium text-[#1a1a1a] hover:bg-[#f3efe6] transition-colors cursor-pointer"
    >
      {children}
      <ExternalLink className="h-3 w-3" />
    </a>
  );
}

function InlineLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="font-semibold text-[#1a1a1a] underline decoration-[#e0dbd0] decoration-2 underline-offset-2 hover:decoration-[#1a1a1a] cursor-pointer"
    >
      {children}
    </a>
  );
}

function InstructionsList({ items }: { items: React.ReactNode[] }) {
  return (
    <ol className="space-y-2 text-sm text-[#3a3a3a]">
      {items.map((item, i) => (
        <li key={i} className="flex gap-2.5">
          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-[#f3efe6] text-[10px] font-semibold text-[#8a8578]">
            {i + 1}
          </span>
          <span className="pt-0.5">{item}</span>
        </li>
      ))}
    </ol>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 rounded-md bg-[#fef2f2] border border-[#fecaca] p-3 text-xs text-[#991b1b]">
      <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
      <span>{message}</span>
    </div>
  );
}

function InfoCallout({ title, body }: { title: string; body: React.ReactNode }) {
  return (
    <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900 space-y-1">
      <p className="font-semibold">{title}</p>
      <div>{body}</div>
    </div>
  );
}

// =============================================================================
// Main wizard
// =============================================================================

// All possible step ids across providers. Each provider picks its subset.
type StepId =
  | "intro" // generic welcome + instructions on where to create the bot
  | "discord-intents" // Discord-only: enable Message Content Intent + invite bot
  | "slack-manifest" // Slack-only: copy manifest and install to workspace
  | "token" // paste token(s)
  | "connecting" // async wait for bot to start
  | "pair" // DM the bot and paste pairing code
  | "done";

const PROVIDER_STEPS: Record<Provider, StepId[]> = {
  telegram: ["intro", "token", "connecting", "pair", "done"],
  discord: ["intro", "discord-intents", "token", "connecting", "pair", "done"],
  slack: ["slack-manifest", "token", "connecting", "pair", "done"],
};

export function BotSetupWizard({
  mode,
  provider,
  agentId,
  botUsername,
  onComplete,
  onCancel,
}: BotSetupWizardProps) {
  const api = useApi();
  const callRpc = useGatewayRpcMutation();

  // In link-only mode the bot is already set up and we just need a pairing
  // code from the member. Skip straight to the pair step.
  const steps = mode === "link-only" ? (["pair", "done"] as StepId[]) : PROVIDER_STEPS[provider];
  const [stepIndex, setStepIndex] = useState(0);
  const step = steps[stepIndex];

  const [token, setToken] = useState("");
  const [slackAppToken, setSlackAppToken] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [manifestCopied, setManifestCopied] = useState(false);
  const [connectingNote, setConnectingNote] = useState("");

  // Whether the wizard has written an account row to openclaw.json that has
  // not yet been confirmed working. If the user cancels, the bot reports
  // an error, or polling times out, we DELETE the account row so they
  // don't end up with broken half-configured state on EFS that they can't
  // fix from the UI. Cleared once we successfully reach the pair step.
  const dirtyConfig = useRef(false);
  const rollbackInFlight = useRef(false);
  // Set by the Cancel button on the connecting step; awaitChannelReady polls it.
  const cancelConnect = useRef(false);

  const rollbackAccount = async () => {
    if (!dirtyConfig.current || rollbackInFlight.current) return;
    rollbackInFlight.current = true;
    try {
      await api.del(`/channels/${provider}/${agentId}`);
      dirtyConfig.current = false;
    } catch (e) {
      // Best-effort cleanup; surface to console but don't block UI.
      // eslint-disable-next-line no-console
      console.warn("Channel rollback failed", e);
    } finally {
      rollbackInFlight.current = false;
    }
  };

  const label = PROVIDER_LABELS[provider];

  // Progress indicator excludes the terminal "done" step so the dots don't
  // feel over after linking succeeds.
  const progressSteps = steps.filter((s) => s !== "done");
  const progressIndex = Math.min(stepIndex, progressSteps.length - 1);

  const clearError = () => setError(null);
  const goNext = () => {
    clearError();
    setStepIndex((i) => Math.min(i + 1, steps.length - 1));
  };
  const goBack = () => {
    clearError();
    setStepIndex((i) => Math.max(i - 1, 0));
  };

  const copyManifest = async () => {
    try {
      await navigator.clipboard.writeText(SLACK_APP_MANIFEST);
      setManifestCopied(true);
      setTimeout(() => setManifestCopied(false), 2000);
    } catch {
      // clipboard API can fail in insecure contexts — no-op, user can select manually
    }
  };

  // ---------------------------------------------------------------------------
  // Async actions
  // ---------------------------------------------------------------------------

  const submitTokenAndConnect = async () => {
    const tokenErr = validateToken(provider, token);
    if (tokenErr) {
      setError(tokenErr);
      return;
    }
    if (provider === "slack") {
      const appErr = validateSlackAppToken(slackAppToken);
      if (appErr) {
        setError(appErr);
        return;
      }
    }

    setBusy(true);
    clearError();
    // Analytics: fire on user intent (submit button click) regardless of
    // whether the bot ultimately comes up. This is the "the user tried
    // to link a $provider bot" signal — drop-off between this and the
    // existing `channel_connected` capture (fired from submitPairingCode
    // on success) tells us where setup is breaking.
    capture("channel_link_submitted", { channel_type: provider });
    // Move UI to the connecting state immediately so the user sees progress.
    cancelConnect.current = false;
    setConnectingNote("");
    setStepIndex(steps.indexOf("connecting"));

    // OpenClaw stores per-account runtime state in a Map that PERSISTS
    // across restarts: src/gateway/server-channels.ts:246-256 setRuntime
    // does { ...current, ...patch } so stale fields stick around. When
    // the previous attempt failed it left lastError="not configured" on
    // the runtime entry. The new restart only clears it when the new
    // instance successfully starts (server-channels.ts:392 lastError:
    // null) — until then, polling channels.status returns the stale
    // error and we'd false-positive a failure.
    //
    // To detect when the restart has actually completed, capture
    // lastStartAt before the PATCH and only trust the snapshot once it
    // has advanced. If the previous instance never ran, initialStartAt
    // will be null and any defined value means the new instance started.
    type AccountSnapshot = {
      accountId?: string;
      running?: boolean;
      connected?: boolean;
      lastError?: string | null;
      lastStartAt?: number | null;
    };
    let initialStartAt: number | null = null;
    try {
      const preStatus = (await callRpc("channels.status", { probe: false })) as {
        channelAccounts?: Record<string, AccountSnapshot[]>;
      };
      const preAccounts = preStatus?.channelAccounts?.[provider] ?? [];
      const preEntry = preAccounts.find((a) => a.accountId === agentId);
      initialStartAt = preEntry?.lastStartAt ?? null;
    } catch {
      // If we can't read pre-state, treat as null — first defined value
      // we see post-PATCH counts as a fresh start.
    }

    try {
      // Field name varies per provider:
      //   telegram → botToken
      //   discord  → token       (NOT botToken — confirmed in
      //                            extensions/discord/src/shared.ts:62,115:
      //                            clearBaseFields=['token','name'],
      //                            isConfigured: account.token)
      //   slack    → botToken + appToken (socket mode)
      const accountCfg: Record<string, unknown> =
        provider === "slack"
          ? {
              mode: "socket",
              appToken: slackAppToken.trim(),
              botToken: token.trim(),
              dmPolicy: "pairing",
            }
          : provider === "discord"
            ? {
                token: token.trim(),
                dmPolicy: "pairing",
              }
            : {
                botToken: token.trim(),
                dmPolicy: "pairing",
              };
      // Single PATCH with enabled + accounts. Channels ship enabled:true at
      // provision time, so this is a pure hot reload of the plugin with the
      // new account — seconds, not the full gateway restart.
      const patch: Record<string, unknown> = {
        channels: {
          [provider]: {
            enabled: true,
            accounts: { [agentId]: accountCfg },
          },
        },
      };
      await api.patchConfig(patch);
      // Mark the config dirty so any failure / cancel from this point on
      // rolls the account row back instead of leaving broken state on EFS.
      dirtyConfig.current = true;

      // Wait for the channel to actually come up. OpenClaw 6.6 defers the
      // reload until in-flight work drains (up to ~300s on a busy box), so we
      // wait past that ceiling, surface the wait, and key success on a real
      // `connected` transition — never the bare `running:true` the gateway sets
      // before auth runs (so a bad token surfaces as a real error, not a false
      // success). `initialStartAt` lets the poller ignore stale pre-restart state.
      const outcome = await awaitChannelReady({
        callRpc,
        provider,
        accountId: agentId,
        initialStartAt,
        onNote: setConnectingNote,
        shouldCancel: () => cancelConnect.current,
      });
      if (outcome.status === "ready") {
        dirtyConfig.current = false;
        setStepIndex(steps.indexOf("pair"));
        return;
      }
      if (outcome.status === "error") {
        setError(`Bot failed to start: ${outcome.lastError}`);
        await rollbackAccount();
        setStepIndex(steps.indexOf("token"));
        return;
      }
      if (outcome.status === "cancelled") {
        // User backed out of the wait. Keep the saved account (it may yet come
        // up); just return to the token step without an error or rollback.
        dirtyConfig.current = false;
        setStepIndex(steps.indexOf("token"));
        return;
      }
      // timeout: the reload is taking longer than our ceiling. Do NOT roll back
      // — the account is valid and the deferred reload may still land. Keep it
      // and tell the user it will finish connecting in the background.
      dirtyConfig.current = false;
      setError(
        "Setup is taking longer than expected — your container is busy. Your bot is saved and " +
          "will finish connecting in the background; check the Channels status in a few minutes.",
      );
      setStepIndex(steps.indexOf("token"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      // patchConfig itself may have thrown; rollback is a no-op if nothing
      // was committed (dirtyConfig.current === false).
      await rollbackAccount();
      setStepIndex(steps.indexOf("token"));
    } finally {
      setBusy(false);
    }
  };

  const submitPairingCode = async () => {
    const trimmed = code.trim().toUpperCase();
    if (trimmed.length < 4) {
      setError("Pairing code is required.");
      return;
    }
    setBusy(true);
    clearError();
    // Analytics: link-only flow (members pairing their identity to an
    // already-configured bot) — mirror `submitTokenAndConnect` so the
    // event fires once per "user hit submit" regardless of which flow
    // they're in.
    if (mode === "link-only") {
      capture("channel_link_submitted", { channel_type: provider });
    }
    try {
      const result = (await api.post(`/channels/link/${provider}/complete`, {
        agent_id: agentId,
        code: trimmed,
      })) as { status: string; peer_id: string };
      capture("channel_connected", { provider });
      setStepIndex(steps.indexOf("done"));
      onComplete({ peer_id: result.peer_id });
    } catch (e) {
      const err = e as { status?: number; message?: string };
      if (err.status === 404) {
        setError("Code expired or already used. DM the bot again to get a new code.");
      } else if (err.status === 409) {
        setError("This account is already linked to another member.");
      } else {
        setError(err.message || String(e));
      }
    } finally {
      setBusy(false);
    }
  };

  // ---------------------------------------------------------------------------
  // Per-step render helpers
  // ---------------------------------------------------------------------------

  const renderIntro = () => {
    const items: Record<Provider, React.ReactNode[]> = {
      telegram: [
        <>
          Open Telegram on your phone or desktop and start a chat with{" "}
          <InlineLink href="https://t.me/BotFather">@BotFather</InlineLink>.
        </>,
        <>
          Send <code className="px-1 py-0.5 rounded bg-[#f3efe6] font-mono text-[11px]">/newbot</code>{" "}
          and follow BotFather&apos;s prompts — pick a display name, then a username
          (must end in <span className="font-mono">bot</span>).
        </>,
        <>
          BotFather will reply with a message containing your{" "}
          <span className="font-semibold">HTTP API token</span>. Copy it — you&apos;ll paste it on
          the next step.
        </>,
      ],
      discord: [
        <>
          Open the{" "}
          <InlineLink href="https://discord.com/developers/applications">
            Discord Developer Portal
          </InlineLink>{" "}
          and click <span className="font-semibold">New Application</span>. Give it a name — this
          is what users will see.
        </>,
        <>
          In the left sidebar, go to <span className="font-semibold">Bot</span>, then click{" "}
          <span className="font-semibold">Reset Token</span> →{" "}
          <span className="font-semibold">Yes, do it!</span> →{" "}
          <span className="font-semibold">Copy</span>. Save the token somewhere safe — Discord
          won&apos;t show it again.
        </>,
        <>You&apos;ll need a couple more settings on the next step before we can connect.</>,
      ],
      slack: [],
    };
    return (
      <div className="space-y-4">
        <div>
          <h4 className="text-sm font-semibold text-[#1a1a1a]">Create your {label} bot</h4>
          <p className="mt-1 text-xs text-[#8a8578]">
            We&apos;ll walk you through {steps.length - 1} quick steps.
          </p>
        </div>
        <InstructionsList items={items[provider]} />
        <ExternalLinkButton href={PROVIDER_CREATE_URL[provider]}>
          {provider === "telegram" ? "Open @BotFather" : "Open Discord Developer Portal"}
        </ExternalLinkButton>
      </div>
    );
  };

  const renderDiscordIntents = () => (
    <div className="space-y-4">
      <div>
        <h4 className="text-sm font-semibold text-[#1a1a1a]">Finish configuring your Discord app</h4>
        <p className="mt-1 text-xs text-[#8a8578]">
          Two non-obvious steps that are easy to miss.
        </p>
      </div>
      <InfoCallout
        title="Enable Message Content Intent"
        body={
          <>
            Open your app in the{" "}
            <InlineLink href="https://discord.com/developers/applications">
              Developer Portal
            </InlineLink>
            , click <span className="font-semibold">Bot</span> in the sidebar, scroll down to{" "}
            <span className="font-semibold">Privileged Gateway Intents</span> and toggle{" "}
            <InlineLink href="https://discord.com/developers/docs/topics/gateway#privileged-intents">
              Message Content Intent
            </InlineLink>{" "}
            on. Without this, Discord will silently drop message content and pairing will never
            work.
          </>
        }
      />
      <InstructionsList
        items={[
          <>
            Back in the{" "}
            <InlineLink href="https://discord.com/developers/applications">
              Developer Portal
            </InlineLink>
            , open your app and go to{" "}
            <span className="font-semibold">OAuth2</span> →{" "}
            <span className="font-semibold">URL Generator</span> in the sidebar.
          </>,
          <>
            Under <span className="font-semibold">Scopes</span>, check{" "}
            <span className="font-mono">bot</span>. (No bot permissions need to be selected for
            basic DMs.) Copy the generated URL at the bottom.
          </>,
          <>
            Open that URL in a new tab and invite the bot to a Discord server you own. You can{" "}
            <InlineLink href="https://support.discord.com/hc/en-us/articles/204849977-How-do-I-create-a-server">
              create a private server
            </InlineLink>{" "}
            just for testing if you don&apos;t have one.
          </>,
        ]}
      />
    </div>
  );

  const renderSlackManifest = () => (
    <div className="space-y-4">
      <div>
        <h4 className="text-sm font-semibold text-[#1a1a1a]">Create your Slack app</h4>
        <p className="mt-1 text-xs text-[#8a8578]">
          We give Slack a manifest that pre-configures everything — scopes, events, socket mode.
        </p>
      </div>
      <InstructionsList
        items={[
          <>
            Open{" "}
            <InlineLink href="https://api.slack.com/apps?new_app=1">
              api.slack.com/apps
            </InlineLink>{" "}
            and click <span className="font-semibold">Create New App</span> →{" "}
            <span className="font-semibold">From an app manifest</span>.
          </>,
          <>
            Pick the workspace where your bot will live, then paste the manifest below when
            prompted and click <span className="font-semibold">Create</span>.
          </>,
          <>
            On your new app&apos;s page, click <span className="font-semibold">Install to Workspace</span>
            {" "}and authorize the scopes.
          </>,
          <>
            Go to <span className="font-semibold">Basic Information</span> → scroll to{" "}
            <span className="font-semibold">App-Level Tokens</span> →{" "}
            <span className="font-semibold">Generate Token and Scopes</span>, name it{" "}
            <span className="font-mono">isol8</span>, add the{" "}
            <span className="font-mono">connections:write</span> scope, click{" "}
            <span className="font-semibold">Generate</span>, copy the <span className="font-mono">xapp-</span>
            {" "}token.
          </>,
          <>
            Go to <span className="font-semibold">OAuth &amp; Permissions</span> and copy the{" "}
            <span className="font-semibold">Bot User OAuth Token</span> (starts with{" "}
            <span className="font-mono">xoxb-</span>).
          </>,
        ]}
      />
      <div className="rounded-md border border-[#e0dbd0] bg-white">
        <div className="flex items-center justify-between px-3 py-2 border-b border-[#e0dbd0]">
          <span className="text-xs font-semibold text-[#1a1a1a]">App manifest</span>
          <button
            type="button"
            onClick={copyManifest}
            className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-[#8a8578] hover:bg-[#f3efe6] hover:text-[#1a1a1a] transition-colors cursor-pointer"
            aria-label="Copy manifest"
          >
            {manifestCopied ? (
              <>
                <Check className="h-3 w-3" /> Copied
              </>
            ) : (
              <>
                <Copy className="h-3 w-3" /> Copy
              </>
            )}
          </button>
        </div>
        <pre className="whitespace-pre-wrap font-mono text-[10px] p-3 max-h-52 overflow-y-auto">
          {SLACK_APP_MANIFEST}
        </pre>
      </div>
      <ExternalLinkButton href={PROVIDER_CREATE_URL.slack}>
        Open api.slack.com/apps
      </ExternalLinkButton>
    </div>
  );

  const renderToken = () => {
    const placeholder =
      provider === "telegram"
        ? "123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        : provider === "slack"
          ? "xoxb-..."
          : "Your bot token";
    const labelText =
      provider === "slack" ? "Bot User OAuth Token (xoxb-...)" : `${label} bot token`;
    return (
      <div className="space-y-4">
        <div>
          <h4 className="text-sm font-semibold text-[#1a1a1a]">Paste your bot token</h4>
          <p className="mt-1 text-xs text-[#8a8578]">
            Tokens are stored encrypted in your container and never logged.
          </p>
        </div>
        {provider === "slack" && (
          <label className="block">
            <span className="text-xs font-semibold text-[#1a1a1a]">
              App-Level Token (xapp-...)
            </span>
            <input
              type="password"
              value={slackAppToken}
              onChange={(e) => {
                setSlackAppToken(e.target.value);
                clearError();
              }}
              placeholder="xapp-1-A..."
              className="mt-1 w-full rounded-md border border-[#e0dbd0] bg-white px-3 py-2 text-sm font-mono focus:border-[#1a1a1a] focus:outline-none"
              aria-label="App-Level Token"
              autoComplete="off"
              spellCheck={false}
            />
          </label>
        )}
        <label className="block">
          <span className="text-xs font-semibold text-[#1a1a1a]">{labelText}</span>
          <input
            type="password"
            value={token}
            onChange={(e) => {
              setToken(e.target.value);
              clearError();
            }}
            placeholder={placeholder}
            className="mt-1 w-full rounded-md border border-[#e0dbd0] bg-white px-3 py-2 text-sm font-mono focus:border-[#1a1a1a] focus:outline-none"
            aria-label={labelText}
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        {error && <ErrorBanner message={error} />}
      </div>
    );
  };

  const renderConnecting = () => (
    <div className="py-8 flex flex-col items-center justify-center gap-3 text-center">
      <Loader2 className="h-6 w-6 animate-spin text-[#8a8578]" />
      <div>
        <p className="text-sm font-semibold text-[#1a1a1a]">Connecting to {label}…</p>
        <p className="mt-1 text-xs text-[#8a8578]">
          {connectingNote ||
            "Your container is starting the bot. This can take a few minutes if your agent is busy."}
        </p>
      </div>
      <button
        type="button"
        onClick={() => {
          cancelConnect.current = true;
        }}
        className="text-xs text-[#8a8578] underline underline-offset-2 hover:text-[#1a1a1a]"
      >
        Cancel
      </button>
    </div>
  );

  const renderPair = () => {
    // For link-only mode (members pairing to a bot they didn't create), the
    // username is essential — there's no other way for them to know which
    // bot to DM. Surface it as a copyable @handle when known. For create
    // mode the user just typed the username into BotFather so it's
    // optional context.
    const handle = botUsername
      ? botUsername.startsWith("@") ? botUsername : `@${botUsername}`
      : null;
    const handleNode = handle ? (
      <span className="font-mono font-semibold text-[#1a1a1a]">{handle}</span>
    ) : null;

    const dmInstructions: Record<Provider, React.ReactNode> = {
      telegram: handleNode ? (
        <>
          Open Telegram, search for {handleNode}, and send it any message. It will reply with an
          8-character pairing code.
        </>
      ) : (
        <>
          Open Telegram on your phone, find your new bot by its username (the one you set in
          BotFather), and send it any message. It will reply with an 8-character pairing code.
        </>
      ),
      discord: handleNode ? (
        <>
          Open Discord, find {handleNode} in the server it was added to, right-click and choose{" "}
          <span className="font-semibold">Message</span>. Send any message — it will reply with an
          8-character pairing code.
        </>
      ) : (
        <>
          Open Discord, find your bot in the server you invited it to, right-click its name, and
          choose <span className="font-semibold">Message</span>. Send any message — it will reply
          with an 8-character pairing code.
        </>
      ),
      slack: handleNode ? (
        <>
          Open Slack, find {handleNode} under <span className="font-semibold">Apps</span> in the
          sidebar, and send it any direct message. It will reply with an 8-character pairing code.
        </>
      ) : (
        <>
          Open Slack, find your bot under <span className="font-semibold">Apps</span> in the
          sidebar, and send it any direct message. It will reply with an 8-character pairing code.
        </>
      ),
    };
    return (
      <div className="space-y-4">
        <div>
          <h4 className="text-sm font-semibold text-[#1a1a1a]">Link your account to the bot</h4>
          <p className="mt-1 text-xs text-[#8a8578]">
            This lets the bot recognize you as an authorized user. Codes expire after 1 hour.
          </p>
        </div>
        <p className="text-sm text-[#3a3a3a]">{dmInstructions[provider]}</p>
        <label className="block">
          <span className="text-xs font-semibold text-[#1a1a1a]">Pairing code</span>
          <input
            type="text"
            value={code}
            onChange={(e) => {
              setCode(e.target.value.toUpperCase());
              clearError();
            }}
            placeholder="ABC12345"
            maxLength={8}
            className="mt-1 w-full rounded-md border border-[#e0dbd0] bg-white px-3 py-2 text-lg font-mono tracking-widest uppercase focus:border-[#1a1a1a] focus:outline-none"
            aria-label="Pairing code"
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        {error && <ErrorBanner message={error} />}
      </div>
    );
  };

  const renderDone = () => (
    <div className="py-8 flex flex-col items-center justify-center gap-3 text-center">
      <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[#e8f5e9]">
        <Check className="h-5 w-5 text-[#2d8a4e]" />
      </div>
      <div>
        <p className="text-sm font-semibold text-[#1a1a1a]">Linked</p>
        <p className="mt-1 text-xs text-[#8a8578]">You can now DM the bot and it will respond.</p>
      </div>
    </div>
  );

  // ---------------------------------------------------------------------------
  // Footer (cancel / back / next)
  // ---------------------------------------------------------------------------

  // Cancel handler that rolls back any half-committed config (e.g. user
  // pasted a bad token, bot is starting, they bail before it succeeds).
  const handleCancel = async () => {
    if (dirtyConfig.current) {
      await rollbackAccount();
    }
    onCancel();
  };

  const renderFooter = () => {
    if (step === "connecting" || step === "done") {
      return null;
    }
    const canGoBack = stepIndex > 0 && !busy;
    let primaryLabel = "Next";
    let primaryAction: () => void | Promise<void> = goNext;
    let primaryDisabled = busy;

    if (step === "token") {
      primaryLabel = "Connect";
      primaryAction = submitTokenAndConnect;
      primaryDisabled =
        busy ||
        !token.trim() ||
        (provider === "slack" && !slackAppToken.trim());
    } else if (step === "pair") {
      primaryLabel = "Link";
      primaryAction = submitPairingCode;
      primaryDisabled = busy || code.trim().length < 4;
    }

    return (
      <div className="flex items-center justify-between pt-4 border-t border-[#e0dbd0]">
        <Button
          variant="ghost"
          size="sm"
          onClick={canGoBack ? goBack : handleCancel}
          disabled={busy}
          className="text-[#8a8578] cursor-pointer"
        >
          {canGoBack ? (
            <>
              <ArrowLeft className="h-3.5 w-3.5 mr-1" />
              Back
            </>
          ) : (
            "Cancel"
          )}
        </Button>
        <Button onClick={primaryAction} disabled={primaryDisabled} className="cursor-pointer">
          {busy && <Loader2 className="h-3 w-3 animate-spin mr-1" />}
          {primaryLabel}
        </Button>
      </div>
    );
  };

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div className="p-6 space-y-4 w-[28rem] max-w-[calc(100vw-2rem)]">
      <div className="flex items-center justify-between">
        <h3 className="text-base font-semibold text-[#1a1a1a]">
          {mode === "create" ? `Set up ${label}` : `Link your ${label} account`}
        </h3>
        {progressSteps.length > 1 && (
          <StepIndicator current={progressIndex} total={progressSteps.length} />
        )}
      </div>

      <div className="min-h-[16rem]">
        {step === "intro" && renderIntro()}
        {step === "discord-intents" && renderDiscordIntents()}
        {step === "slack-manifest" && renderSlackManifest()}
        {step === "token" && renderToken()}
        {step === "connecting" && renderConnecting()}
        {step === "pair" && renderPair()}
        {step === "done" && renderDone()}
      </div>

      {renderFooter()}
    </div>
  );
}
