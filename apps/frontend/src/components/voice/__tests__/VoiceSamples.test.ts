import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import path from "node:path";
import { expect, it } from "vitest";
import { PHONE_VOICES, voiceSamplePath } from "@lightbulb/shared";

it("ships a genuine decoded PCM fixture for every advertised voice and language", () => {
  const base = path.resolve(process.cwd(), "public/voice-samples");
  const manifest = JSON.parse(readFileSync(path.join(base, "manifest.json"), "utf8"));
  expect(manifest.model).toBe("gpt-live-1");
  expect(manifest.samples).toHaveLength(6);
  expect(manifest.totalBilledSeconds).toBeLessThanOrEqual(90);
  expect(manifest.providerCostUSD).toBeLessThanOrEqual(1);
  for (const { id } of PHONE_VOICES) for (const language of ["en", "es"] as const) {
    const file = path.basename(voiceSamplePath(id, language));
    const sample = manifest.samples.find((s: { file: string }) => s.file === file);
    expect(sample).toMatchObject({ voice: id, language });
    const wav = readFileSync(path.join(base, file));
    expect(createHash("sha256").update(wav).digest("hex")).toBe(sample.sha256);
    expect(wav.toString("ascii", 0, 4)).toBe("RIFF");
    expect(wav.toString("ascii", 8, 12)).toBe("WAVE");
    expect(wav.readUInt16LE(20)).toBe(1); // PCM
    expect(wav.readUInt16LE(22)).toBe(1); // mono
    expect(wav.readUInt32LE(24)).toBe(24_000);
    expect(wav.readUInt16LE(34)).toBe(16);
    expect(wav.readUInt32LE(40) / 48_000).toBeCloseTo(sample.seconds);
    expect(sample.seconds).toBeGreaterThan(1);
    expect(sample.seconds).toBeLessThanOrEqual(15);
    expect(wav.subarray(44).some((byte) => byte !== 0)).toBe(true);
    expect(sample.transcript).toContain(language === "en" ? "AI assistant" : "inteligencia artificial");
  }
});
