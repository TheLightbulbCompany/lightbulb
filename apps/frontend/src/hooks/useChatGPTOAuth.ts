"use client";

import { useApi } from "@/lib/api";
import { useChatGPTOAuth as useChatGPTOAuthShared, type OAuthState } from "@lightbulb/shared";

// The hook body lives in @lightbulb/shared (extracted so the mobile app could
// share it). This module is the web platform wrapper: it supplies the real
// useApi() client and preserves the historical no-arg call signature +
// OAuthState export so every existing web consumer (ChatGPTOAuthStep) keeps
// working unchanged.
export type { OAuthState };

export function useChatGPTOAuth() {
  return useChatGPTOAuthShared(useApi());
}
