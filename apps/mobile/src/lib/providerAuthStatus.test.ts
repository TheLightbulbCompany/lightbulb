import { resolveProviderStatus } from "./providerAuthStatus";

test("Muse health uses the Zen credential rather than a Bedrock credential", () => {
  expect(resolveProviderStatus("opencode_zen", null, [
    { provider: "opencode-zen", displayName: "Muse", status: "static" },
  ])).toBe("static");
  expect(resolveProviderStatus("opencode_zen", null, [
    { provider: "amazon-bedrock", displayName: "Claude", status: "static" },
  ])).toBeUndefined();
});
