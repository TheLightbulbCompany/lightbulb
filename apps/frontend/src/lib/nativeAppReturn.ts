import { DEEP_LINK_SCHEME } from "@/lib/domain";

export const NATIVE_APP_FALLBACK_MS = 1500;

const API_URL = process.env.NEXT_PUBLIC_API_URL || "https://api-dev.thelightbulb.company/api/v1";

/** Detect iOS Safari, including iPadOS when it requests a desktop user agent. */
export function isIosDevice(userAgent: string, platform: string, maxTouchPoints: number): boolean {
  return /iPad|iPhone|iPod/.test(userAgent) || (platform === "MacIntel" && maxTouchPoints > 1);
}

type NativeAppReturnOptions = {
  userAgent: string;
  platform: string;
  maxTouchPoints: number;
  appUrl?: string;
  openApp: (url: string) => void;
  continueWeb: () => void;
  scheduleFallback: (callback: () => void, delayMs: number) => number;
};

type TokenGetter = () => Promise<string | null>;
type FetchLike = (
  input: string,
  init: { method: "POST"; headers: { Authorization: string } },
) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;

/**
 * Hand an iOS browser back to the installed app without stranding anyone who
 * does not have it. Other platforms continue to the web workspace immediately.
 */
export function beginNativeAppReturn(options: NativeAppReturnOptions): number | null {
  if (!isIosDevice(options.userAgent, options.platform, options.maxTouchPoints)) {
    options.continueWeb();
    return null;
  }

  options.openApp(options.appUrl ?? `${DEEP_LINK_SCHEME}://`);
  return options.scheduleFallback(options.continueWeb, NATIVE_APP_FALLBACK_MS);
}

/** Exchange the authenticated browser session for a short-lived, single-use
 * Clerk ticket that the native client can consume into its own session. */
export async function createNativeAppSignInUrl(
  _webDestination: string,
  getToken: TokenGetter,
  fetchImpl: FetchLike = fetch,
): Promise<string> {
  const browserToken = await getToken();
  if (!browserToken) {
    throw new Error("Could not prepare the native sign-in handoff");
  }

  const response = await fetchImpl(`${API_URL}/auth/desktop/sign-in-token`, {
    method: "POST",
    headers: { Authorization: `Bearer ${browserToken}` },
  });
  if (!response.ok) {
    throw new Error("Could not prepare the native sign-in handoff");
  }

  const payload = await response.json();
  const ticket =
    typeof payload === "object" && payload !== null && "token" in payload
      ? (payload as { token?: unknown }).token
      : null;
  if (typeof ticket !== "string" || !ticket) {
    throw new Error("Could not prepare the native sign-in handoff");
  }

  return `${DEEP_LINK_SCHEME}://auth?ticket=${encodeURIComponent(ticket)}`;
}
