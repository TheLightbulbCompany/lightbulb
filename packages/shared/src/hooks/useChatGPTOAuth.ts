import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/error";
import type { ApiMethods } from "../api/client";

/**
 * ChatGPT OAuth — device-code flow.
 *
 * Extracted verbatim from the web frontend's useChatGPTOAuth (mobile-app
 * plan Slice 4): the platform wrapper injects its own `api` (web's
 * `useApi()`, mobile's `createApiClient(makeAdapter(...))`); everything
 * below is shared behavior.
 *
 * 1. start() → backend requests user_code + verification_uri from OpenAI.
 * 2. UI shows user the URL + code. User opens URL, signs in, enters code.
 * 3. We poll /poll every `interval` seconds. While the user hasn't
 *    completed sign-in, OpenAI returns "pending"; once they have,
 *    backend exchanges for tokens and stores them. /poll then returns
 *    "completed".
 * 4. disconnect() → revoke + clear EFS auth.json.
 */
export type OAuthState =
  | { status: "idle" }
  | {
      status: "pending";
      userCode: string;
      verificationUri: string;
      expiresAt: number;
    }
  | { status: "completed"; accountId: string | null }
  /** The workspace isn't on a paid plan: /start refused (402) before any OpenAI sign-in began. */
  | { status: "plan_required" }
  | { status: "error"; message: string };

type StartResponse = {
  user_code: string;
  verification_uri: string;
  expires_in: number;
  interval: number;
};

type PollResponse =
  | { status: "pending" }
  | { status: "completed"; account_id: string | null };

type StatusResponse = {
  connected: boolean;
  account_id: string | null;
};

const DEFAULT_POLL_INTERVAL_S = 5;

const BASE_PATH = "/oauth/chatgpt";

export function useChatGPTOAuth(api: Pick<ApiMethods, "get" | "post">) {
  const [state, setState] = useState<OAuthState>({ status: "idle" });
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollIntervalSec = useRef<number>(DEFAULT_POLL_INTERVAL_S);
  // Guards the on-mount status-probe + auto-start sequence so React 19
  // StrictMode's intentional double-invoke of effects doesn't fire two
  // /status calls (and worse, two /start device-code requests).
  const startedRef = useRef(false);

  const stopPolling = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  const start = useCallback(async () => {
    stopPolling();
    setState({ status: "idle" });
    try {
      const r = (await api.post(`${BASE_PATH}/start`, {})) as StartResponse;
      pollIntervalSec.current = r.interval || DEFAULT_POLL_INTERVAL_S;
      setState({
        status: "pending",
        userCode: r.user_code,
        verificationUri: r.verification_uri,
        expiresAt: Date.now() + r.expires_in * 1000,
      });
    } catch (e: unknown) {
      // Backend returns 409 (OAuthAlreadyActiveError) when an active token row
      // already exists for this user. The auto-fire from ChatGPTOAuthStep
      // hits this when the wizard re-mounts on a stale ?provider=chatgpt_oauth
      // URL — e.g. browser back/history restore for an already-onboarded user.
      // The user's tokens are fine; treat the 409 as completion so the parent
      // advances out of the wizard (which strips ?provider= via the
      // providerStepDone effect in ProvisioningStepper).
      if (e instanceof ApiError && e.status === 409) {
        setState({ status: "completed", accountId: null });
        return;
      }
      if (e instanceof ApiError && e.status === 402) {
        setState({ status: "plan_required" });
        return;
      }
      const message = e instanceof Error ? e.message : "OAuth start failed";
      setState({ status: "error", message });
    }
  }, [api, stopPolling]);

  const disconnect = useCallback(async () => {
    stopPolling();
    try {
      await api.post(`${BASE_PATH}/disconnect`, {});
      setState({ status: "idle" });
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : "Disconnect failed";
      setState({ status: "error", message });
      // Rethrow so the button that asked can say it failed instead of moving on as if disconnected.
      throw e;
    }
  }, [api, stopPolling]);

  // Poll while in pending state. Interval comes from /start's response
  // (defaults to 5s). Stops once we transition to completed/error.
  useEffect(() => {
    if (state.status !== "pending") return;
    intervalRef.current = setInterval(async () => {
      try {
        const r = (await api.post(
          `${BASE_PATH}/poll`,
          {},
        )) as PollResponse;
        if (r.status === "completed") {
          stopPolling();
          setState({ status: "completed", accountId: r.account_id });
        }
      } catch (e: unknown) {
        const message = e instanceof Error ? e.message : "OAuth poll failed";
        stopPolling();
        setState({ status: "error", message });
      }
    }, pollIntervalSec.current * 1000);

    return () => stopPolling();
  }, [state.status, api, stopPolling]);

  // Stop on unmount.
  useEffect(() => () => stopPolling(), [stopPolling]);

  // On mount: probe /status to recover from a remount mid-flow (or from a
  // re-entry after the user already completed OAuth in a different tab).
  // - connected: true → land in "completed"; the parent's onComplete fires
  //   via the existing useEffect in ChatGPTOAuthStep and the wizard advances.
  // - connected: false → auto-fire start() so the user doesn't have to
  //   click "Connect ChatGPT" again. The 409 fallback in start() covers the
  //   race where /status said disconnected but tokens landed before /start.
  //
  // Concurrency: startedRef prevents the in-flight async work from being
  // duplicated by React 19 StrictMode's intentional remount-replay. We do
  // NOT use a `cancelled` flag in the cleanup — StrictMode runs the
  // cleanup immediately after the first invocation, which would set
  // cancelled=true on the only in-flight attempt; the replayed effect
  // then early-returns at startedRef and the OAuth step would stay on
  // the spinner forever. setState on an unmounted component is a
  // tolerated no-op in React 18+, so no cancellation is needed here.
  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    (async () => {
      try {
        const r = (await api.get(`${BASE_PATH}/status`)) as StatusResponse;
        if (r.connected) {
          setState({ status: "completed", accountId: r.account_id });
          return;
        }
        await start();
      } catch (e: unknown) {
        // Status probe failure: surface as error rather than silently
        // auto-starting, since /status is a required precondition for
        // knowing whether /start would 409.
        const message = e instanceof Error ? e.message : "OAuth status probe failed";
        setState({ status: "error", message });
      }
    })();
    // Run-once on mount by design — re-running on every `api`/`start`
    // identity change would re-fire the device-code request. This package
    // has no react-hooks lint plugin configured (framework-neutral; see
    // eslint.config.js), so no disable directive is needed here.
  }, []);

  return { state, start, disconnect };
}
