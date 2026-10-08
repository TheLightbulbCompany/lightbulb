import { DEEP_LINK_SCHEME } from "@/lib/domain";

export const NATIVE_APP_FALLBACK_MS = 1500;

/** Detect iOS Safari, including iPadOS when it requests a desktop user agent. */
export function isIosDevice(userAgent: string, platform: string, maxTouchPoints: number): boolean {
  return /iPad|iPhone|iPod/.test(userAgent) || (platform === "MacIntel" && maxTouchPoints > 1);
}

type NativeAppReturnOptions = {
  userAgent: string;
  platform: string;
  maxTouchPoints: number;
  openApp: (url: string) => void;
  continueWeb: () => void;
  scheduleFallback: (callback: () => void, delayMs: number) => number;
};

/**
 * Hand an iOS browser back to the installed app without stranding anyone who
 * does not have it. Other platforms continue to the web workspace immediately.
 */
export function beginNativeAppReturn(options: NativeAppReturnOptions): number | null {
  if (!isIosDevice(options.userAgent, options.platform, options.maxTouchPoints)) {
    options.continueWeb();
    return null;
  }

  options.openApp(`${DEEP_LINK_SCHEME}://`);
  return options.scheduleFallback(options.continueWeb, NATIVE_APP_FALLBACK_MS);
}
