"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";

/** The relay the node host's `chrome` (extension) profile pairs with. Upstream publishes it;
 *  the id is the one the bundled node host links to. */
const RELAY_EXTENSION_URL = "https://chromewebstore.google.com/detail/openclaw/kcdjddhmeafeomebliikmbpblkmkfoig";

type Invoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;

/** Which browser agents use on this Mac. Rendered under the Browser switch, which
 *  alone decides whether they may use one. */
export function LocalBrowserSetup() {
  const [invoke, setInvoke] = useState<Invoke | null>(null);
  const [extension, setExtension] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [pairing, setPairing] = useState("");
  useEffect(() => {
    const bridge = (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke;
    if (!bridge) return;
    setInvoke(() => bridge);
    bridge<{ extensionEnabled: boolean } | null | undefined>("local_browser_status").then(
      status => setExtension(Boolean(status?.extensionEnabled)),
      () => setMessage("Update the Lightbulb desktop app to choose a browser."),
    );
  }, []);
  if (!invoke) return null;
  async function toggle(next: boolean) {
    setBusy(true);
    setPairing("");
    try {
      await invoke!("local_browser_configure", { extension: next });
      setExtension(next);
      setMessage("Saved.");
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }
  async function reveal() {
    setBusy(true);
    try { setPairing(await invoke!<string>("local_browser_pairing")); }
    catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }
  return <div className="ml-6 space-y-2 text-sm" role="group" aria-label="Which browser">
    <p className="text-muted-foreground">Agents use their own Chrome window, separate from your everyday profile and its sign-ins.</p>
    <label className="flex items-start gap-2">
      <input type="checkbox" checked={extension} disabled={busy} onChange={event => void toggle(event.target.checked)} />
      Also let agents use tabs you choose in your everyday Chrome
    </label>
    {extension && <div className="space-y-2">
      <p>In your everyday Chrome, add the browser relay extension and choose <strong>Selected tabs</strong> in its settings. The Chrome Web Store lists it as &ldquo;OpenClaw&rdquo;, the open-source project this Mac&apos;s agent host is built on; Lightbulb does not publish its own extension yet. Agents can use only the tabs you move into its tab group; moving a tab out takes it back.</p>
      {/* window.open, not an <a target>: the desktop webview hands window.open to the system browser and drops anchor popups. */}
      <Button variant="outline" onClick={() => window.open(RELAY_EXTENSION_URL, "_blank")}>Open in Chrome Web Store</Button>
      <p>Paste the pairing code into the extension&apos;s manual pairing. Keep it private.</p>
      <Button variant="outline" onClick={reveal} disabled={busy}>Show pairing code</Button>
      {pairing && <textarea aria-label="Private local pairing code" readOnly value={pairing} className="w-full rounded border p-2 font-mono text-xs" onFocus={event => event.target.select()} />}
    </div>}
    {message && <p role="status">{message}</p>}
  </div>;
}
