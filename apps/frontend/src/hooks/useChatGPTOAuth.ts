"use client";

import { useApi } from "@/lib/api";
import {
  useChatGPTOAuth as useChatGPTOAuthShared,
  type OAuthState,
} from "@isol8/shared";

// The hook body lives in @isol8/shared (extracted for the mobile app — see
// docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md). This module is
// the web platform wrapper: it supplies the real useApi() client and
// preserves the historical no-arg call signature + OAuthState export so
// every existing web consumer (ChatGPTOAuthStep, LLMPanel, ChangeProviderModal)
// keeps working unchanged.
export type { OAuthState };

export function useChatGPTOAuth() {
  return useChatGPTOAuthShared(useApi());
}
