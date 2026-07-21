import config from "../fingerprint.config.js";

// Guards the fileHookTransform's contract with @expo/fingerprint's
// undocumented internals: the 'AutolinkingConfig' source-id substring and
// whole-chunk delivery for contents sources. If a future @expo/fingerprint
// bump changes either, these break loudly instead of the filter silently
// never firing — the same zero-error-signal failure class the config exists
// to prevent.

const AUTOLINKING_SOURCE = {
  type: "contents",
  id: "expoAutolinkingConfig:ios",
} as const;

const PNPM_PATH =
  '"podspecDir": "../../node_modules/.pnpm/expo-font@57.0.0_expo@57.0.4_react-native@0.86.0_6jdt44kjaqkp6op2sb26xmpstq/node_modules/expo-font/ios"';

describe("fingerprint.config fileHookTransform", () => {
  const transform = config.fileHookTransform!;

  it("strips the pnpm peer-suffix hash from autolinking-config sources", () => {
    const out = transform(AUTOLINKING_SOURCE, PNPM_PATH, true, "utf8");
    expect(out).toBe(
      '"podspecDir": "../../node_modules/.pnpm/expo-font@57.0.0_expo@57.0.4_react-native@0.86.0/node_modules/expo-font/ios"',
    );
  });

  it("keeps package identities and versions hashing (real-change signal)", () => {
    const out = transform(AUTOLINKING_SOURCE, PNPM_PATH, true, "utf8") as string;
    expect(out).toContain("expo-font@57.0.0");
    expect(out).toContain("expo@57.0.4");
  });

  it("covers both rncore and expo autolinking source ids", () => {
    const out = transform(
      { type: "contents", id: "rncoreAutolinkingConfig:android" },
      PNPM_PATH,
      true,
      "utf8",
    );
    expect(out).not.toContain("6jdt44kjaqkp6op2sb26xmpstq");
  });

  it("passes every other source through untouched", () => {
    expect(
      transform({ type: "contents", id: "packageJson:scripts" }, PNPM_PATH, true, "utf8"),
    ).toBe(PNPM_PATH);
    expect(
      transform({ type: "file", filePath: "ios/Podfile" }, PNPM_PATH, true, "utf8"),
    ).toBe(PNPM_PATH);
    expect(transform(AUTOLINKING_SOURCE, null, true, "utf8")).toBeNull();
  });
});
