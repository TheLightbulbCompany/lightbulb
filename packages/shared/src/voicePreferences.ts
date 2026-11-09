/** Stock GPT-Live samples; caller preferences apply to the next phone session. */
export const PHONE_VOICES = [
  { id: "marin", label: "Marin (default)" },
  { id: "quartz", label: "Quartz" },
  { id: "willow", label: "Willow" },
] as const;
export type PhoneVoice = typeof PHONE_VOICES[number]["id"];
export type SampleLanguage = "en" | "es";
export const phoneVoice = (value: unknown): PhoneVoice =>
  PHONE_VOICES.find((voice) => voice.id === value)?.id ?? "marin";
export const voiceSamplePath = (voice: unknown, language: SampleLanguage) =>
  `/voice-samples/gpt-live-1-${phoneVoice(voice)}-${language === "es" ? "es" : "en"}.wav`;
