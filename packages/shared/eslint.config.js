// Minimal flat config for the framework-neutral shared package.
import tseslint from "typescript-eslint";

export default tseslint.config(...tseslint.configs.recommended, {
  ignores: ["vitest.config.ts"],
  rules: {
    // Platform globals must come through PlatformAdapter or be verified
    // cross-platform (fetch/WebSocket/FormData/Blob exist on both web and
    // RN). DOM-only APIs are banned outright.
    "no-restricted-globals": [
      "error",
      { name: "window", message: "DOM-only — inject via PlatformAdapter." },
      { name: "document", message: "DOM-only — inject via PlatformAdapter." },
      { name: "localStorage", message: "DOM-only — inject via PlatformAdapter." },
      { name: "navigator", message: "DOM-only — inject via PlatformAdapter." },
    ],
  },
});
