export type StorageMode = "browser-local" | "sample";

/** The CRM is intentionally local-first and never provisions a database service. */
export function dbMode(): StorageMode {
  return "browser-local";
}
