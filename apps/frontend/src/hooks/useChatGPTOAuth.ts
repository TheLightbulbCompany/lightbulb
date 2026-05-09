"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, useApi } from "@/lib/api";

/**
 * ChatGPT OAuth — device-code flow.
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

const DEFAULT_POLL_INTERVAL_S = 5;

export function useChatGPTOAuth() {
  const api = useApi();
  const [state, setState] = useState<OAuthState>({ status: "idle" });
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollIntervalSec = useRef<number>(DEFAULT_POLL_INTERVAL_S);

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
      const r = (await api.post("/oauth/chatgpt/start", {})) as StartResponse;
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
      const message = e instanceof Error ? e.message : "OAuth start failed";
      setState({ status: "error", message });
    }
  }, [api, stopPolling]);

  const disconnect = useCallback(async () => {
    stopPolling();
    try {
      await api.post("/oauth/chatgpt/disconnect", {});
      setState({ status: "idle" });
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : "Disconnect failed";
      setState({ status: "error", message });
    }
  }, [api, stopPolling]);

  // Poll while in pending state. Interval comes from /start's response
  // (defaults to 5s). Stops once we transition to completed/error.
  useEffect(() => {
    if (state.status !== "pending") return;
    intervalRef.current = setInterval(async () => {
      try {
        const r = (await api.post(
          "/oauth/chatgpt/poll",
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

  return { state, start, disconnect };
}
