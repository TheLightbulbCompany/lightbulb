"use client";

import { useApi } from "@/lib/api";
import {
  useChatGPTOAuth as useChatGPTOAuthShared,
  type ChatGPTOAuthOptions,
  type OAuthState,
} from "@lightbulb/shared";

// The hook body lives in @lightbulb/shared (extracted so the mobile app could
// share it). This module is the web platform wrapper: it supplies the real
// useApi() client and preserves the historical no-arg call signature +
// OAuthState export so every existing web consumer (ChatGPTOAuthStep,
// LLMPanel, ChangeProviderModal) keeps working unchanged. The opts param
// serves the coding-harness card (second grant, explicit start).
export type { OAuthState };

export function useChatGPTOAuth(opts?: ChatGPTOAuthOptions) {
  return useChatGPTOAuthShared(useApi(), opts);
}
