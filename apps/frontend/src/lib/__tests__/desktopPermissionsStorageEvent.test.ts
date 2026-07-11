import { describe, it, expect } from "vitest";
import {
  isPermissionsStorageEvent,
  DESKTOP_PERMISSIONS_STORAGE_KEY,
} from "../desktopPermissions";

describe("isPermissionsStorageEvent", () => {
  it("matches the permissions key", () => {
    expect(
      isPermissionsStorageEvent({ key: DESKTOP_PERMISSIONS_STORAGE_KEY } as StorageEvent),
    ).toBe(true);
  });
  it("matches key=null (localStorage.clear from another tab)", () => {
    expect(isPermissionsStorageEvent({ key: null } as StorageEvent)).toBe(true);
  });
  it("ignores unrelated keys (e.g. future workspace layout writes)", () => {
    expect(
      isPermissionsStorageEvent({ key: "isol8.workspace.layout.v1:user_x" } as StorageEvent),
    ).toBe(false);
  });
});
