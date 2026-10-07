"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";

type Invoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;

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
    bridge<{ extensionEnabled: boolean }>("local_browser_status").then(
      status => setExtension(status.extensionEnabled),
      () => setMessage("Update the Lightbulb desktop app to configure local browsing."),
    );
  }, []);
  if (!invoke) return null;
  async function save() {
    setBusy(true);
    setPairing("");
    try {
      await invoke!("local_browser_configure", { extension });
      setMessage("Saved. This Mac's agent host is restarting. Ask your agent for browser status on the lightbulb or chrome profile.");
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }
  async function reveal() {
    setBusy(true);
    try { setPairing(await invoke!<string>("local_browser_pairing")); }
    catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }
  return <section className="space-y-3 rounded-lg border p-4" aria-label="Browser on this Mac">
    <h3 className="font-medium">Browser on this Mac</h3>
    <p className="text-sm text-muted-foreground">The lightbulb profile opens a separate local browser. Browser permission is managed independently from shell access.</p>
    <label className="flex items-start gap-2 text-sm">
      <input type="checkbox" checked={extension} disabled={busy} onChange={event => setExtension(event.target.checked)} />
      Allow the installed OpenClaw Chrome extension to connect to this Mac&apos;s agent host.
    </label>
    <Button onClick={save} disabled={busy}>Save browser setup</Button>
    {extension && <div className="space-y-2 text-sm">
      <p>Install the official OpenClaw extension yourself if needed. In its Settings, choose <strong>Selected tabs</strong> before pairing. Only move tabs you want the agent to use into the OpenClaw group; moving them out revokes access.</p>
      <p>After saving, ask your agent for browser status on the chrome profile to start the local relay. Reveal its pairing code and paste it into the extension&apos;s Advanced manual pairing. Keep the code private.</p>
      <Button variant="outline" onClick={reveal} disabled={busy}>Reveal local pairing code</Button>
      {pairing && <textarea aria-label="Private local pairing code" readOnly value={pairing} className="w-full rounded border p-2 font-mono text-xs" onFocus={event => event.target.select()} />}
    </div>}
    <p className="text-sm text-muted-foreground">Chrome MCP attachment is unavailable because its current tab identity cannot be verified safely. Closing or cancelling an operation does not undo browser actions.</p>
    {message && <p role="status" className="text-sm">{message}</p>}
  </section>;
}
