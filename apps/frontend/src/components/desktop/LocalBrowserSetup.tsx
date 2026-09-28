"use client";

import { useEffect, useState } from "react";
import { desktopErrorMessage } from "@/lib/desktopPermissions";

type Invoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;

/** Which browser agents use on this Mac: always their own `lightbulb` Chrome window. Rendered
 *  under the Browser switch, which alone decides whether they may use one. */
export function LocalBrowserSetup() {
  const [desktop, setDesktop] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    const invoke = (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke;
    if (!invoke) return;
    setDesktop(true);
    invoke<{ extensionEnabled: boolean } | null | undefined>("local_browser_status").then(
      status => {
        // Macs that once allowed the everyday-Chrome (`chrome`) profile drop it, once, quietly.
        if (status?.extensionEnabled) invoke("local_browser_configure", { extension: false }).catch(() => {});
      },
      error => setMessage(desktopErrorMessage(error)),
    );
  }, []);
  if (!desktop) return null;
  return <div className="ml-6 space-y-2 text-sm" role="group" aria-label="Which browser">
    <p className="text-muted-foreground">Agents use their own Chrome window, separate from your everyday profile and its sign-ins.</p>
    {message && <p role="status">{message}</p>}
  </div>;
}
