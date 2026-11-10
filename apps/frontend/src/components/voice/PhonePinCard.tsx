"use client";

// Board row 3.29: call the business's number from this mobile, type the PIN and #, and talk as yourself (the orb's
// owner mode). The backend stores the PIN hashed and never sends it back; this field is always empty.
import { useState } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";

export function PhonePinCard() {
  const api = useApi();
  const { data, mutate } = useSWR("/voice/phone-pin", () => api.get("/voice/phone-pin") as Promise<{ set: boolean; mobile: string }>);
  const [mobile, setMobile] = useState<string | null>(null);
  const [pin, setPin] = useState("");
  const [status, setStatus] = useState("");
  const shown = mobile ?? data?.mobile ?? "";
  const valid = /^\d{4,8}$/.test(pin) && shown.trim() !== "";

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!valid) return;
    setStatus("");
    try {
      await mutate(api.put("/voice/phone-pin", { mobile: shown, pin }) as Promise<{ set: boolean; mobile: string }>, { revalidate: false });
      setPin("");
      setMobile(null);
      setStatus("Saved.");
    } catch {
      setStatus("Couldn’t save. Check the number and try again.");
    }
  }

  return <section className="vx-card" aria-labelledby="vx-phone-pin-h">
    <h2 className="vx-h" id="vx-phone-pin-h">Phone PIN</h2>
    <p className="vx-sub">Call your business number from this mobile, type your PIN and then #, and you talk to the assistant as yourself. {data?.set ? "A PIN is set." : "No PIN is set."}</p>
    <form onSubmit={save} style={{ display: "grid", gap: 12 }}>
      <label className="vx-label">Your mobile number
        <input className="vx-field" type="tel" autoComplete="tel" value={shown} onChange={(e) => setMobile(e.target.value)} />
      </label>
      <label className="vx-label">{data?.set ? "New PIN" : "PIN"} (4 to 8 digits)
        <input className="vx-field" type="password" inputMode="numeric" autoComplete="new-password" maxLength={8}
          value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))} />
      </label>
      <button type="submit" className="vx-chip" disabled={!valid}>{data?.set ? "Change PIN" : "Set PIN"}</button>
      {status && <p className="vx-hint" role="status">{status}</p>}
    </form>
  </section>;
}
