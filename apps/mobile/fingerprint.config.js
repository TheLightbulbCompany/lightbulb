/**
 * Fingerprint noise filter — loaded by @expo/fingerprint (and therefore by
 * both the TestFlight build's runtime-version calculation and `eas update`'s
 * publish-time fingerprint).
 *
 * The autolinking-config sources embed node_modules/.pnpm/… podspec paths
 * whose trailing peer-suffix hash churns whenever ANY workspace package's
 * dependencies change — a frontend-only dep bump renames those directories,
 * rotates this app's fingerprint, and silently orphans every installed
 * binary from all subsequent OTA updates (expo-updates skips mismatched
 * runtimes with no error anywhere). Package identities and versions in
 * those same paths still hash, so a REAL native-module change — adding,
 * removing, or upgrading one — still rotates the fingerprint as intended.
 */
const PNPM_PEER_SUFFIX_HASH = /_[a-z0-9]{20,}(?=[/"])/g;

/*
 * app.json's `android` section is hashed into EVERY platform's expoConfig
 * source, so adding Android's package id and adaptive icon would have rotated
 * the iOS runtime and orphaned every installed iPhone from OTAs. Neither key
 * changes JS compatibility: the icon images still hash as Android external
 * files, and a new package id is a new app.
 */
const ANDROID_KEYS_OFF_FINGERPRINT = ['package', 'adaptiveIcon'];

/** @type {import('@expo/fingerprint').Config} */
const config = {
  fileHookTransform: (source, chunk, _isEndOfFile, _encoding) => {
    if (source.type === 'contents' && source.id === 'expoConfig' && chunk !== null) {
      // Input is already key-sorted; parse/stringify keeps that order.
      const config = JSON.parse(chunk.toString());
      for (const key of ANDROID_KEYS_OFF_FINGERPRINT) delete config.android?.[key];
      return JSON.stringify(config);
    }
    if (
      source.type === 'contents' &&
      typeof source.id === 'string' &&
      source.id.includes('AutolinkingConfig') &&
      chunk !== null
    ) {
      return chunk.toString().replace(PNPM_PEER_SUFFIX_HASH, '');
    }
    return chunk;
  },
};

module.exports = config;
