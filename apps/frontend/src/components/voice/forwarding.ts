/**
 * Keep-your-number path (onboarding step 4): the codes an owner dials to forward their own line to the assistant's
 * number. Sources: ~/Lightbulb/super-agent-design/evidence/voice/08-twilio-livekit.md "Numbers at onboarding"
 * (Verizon, AT&T/T-Mobile GSM codes; Google Voice forward-all only; VoIP set in the admin portal).
 * `<n>` is the assistant's 10-digit number. ponytail: landline codes vary by phone company, so it gets a note, not codes.
 */
export type Carrier = "verizon" | "att" | "tmobile" | "landline" | "voip" | "google_voice";
export type ForwardMode = "missed" | "all";
export interface Codes { on: string[]; off: string[] }
interface CarrierCodes { label: string; modes: ForwardMode[]; codes?: Partial<Record<ForwardMode, Codes>>; note?: string }

const GSM: Partial<Record<ForwardMode, Codes>> = {
  missed: { on: ["**61*<n>#", "**67*<n>#"], off: ["##61#", "##67#"] },
  all: { on: ["**21*<n>#"], off: ["##21#"] },
};

export const FORWARDING_CODES: Record<Carrier, CarrierCodes> = {
  verizon: { label: "Verizon", modes: ["missed", "all"], codes: { missed: { on: ["*71<n>"], off: ["*73"] }, all: { on: ["*72<n>"], off: ["*73"] } } },
  att: { label: "AT&T", modes: ["missed", "all"], codes: GSM },
  tmobile: { label: "T-Mobile", modes: ["missed", "all"], codes: GSM },
  landline: { label: "Landline", modes: ["missed", "all"], note: "Ask your phone company to forward busy and unanswered calls to <n>." },
  voip: { label: "VoIP app (Zoom Phone, OpenPhone, RingCentral, OneTalk)", modes: ["missed", "all"], note: "In your admin portal, set forward to external number on busy or no answer, to <n>." },
  google_voice: { label: "Google Voice", modes: ["all"], note: "In Google Voice settings, add <n> as a linked number for calls." },
};

export const IPHONE_NOTE = "On an iPhone, turn off Live Voicemail, or calls go to voicemail first.";
export const CHARGES_NOTE = "Check your plan for forwarding charges.";

/** The on and off codes for a carrier and mode with the number filled in, or null where there is no code to dial. */
export function forwardingCodes(carrier: Carrier, mode: ForwardMode, number: string): Codes | null {
  const c = FORWARDING_CODES[carrier].codes?.[mode];
  return c ? { on: c.on.map((s) => s.replace("<n>", number)), off: c.off } : null;
}
