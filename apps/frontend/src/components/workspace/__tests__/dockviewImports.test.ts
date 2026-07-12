// src/components/workspace/__tests__/dockviewImports.test.ts
// Pins the exact import surface the workspace code relies on. If a future
// dockview bump renames any of these, this file fails before anything else.
import { describe, it, expect } from "vitest";
import {
  DockviewReact,
  themeLight,
  type DockviewReadyEvent,
  type DockviewApi,
  type IDockviewPanelProps,
  type SerializedDockview,
  type AddPanelOptions,
  type DockviewTheme,
} from "dockview-react";

describe("dockview-react import surface", () => {
  it("exports the component, theme object, and types we use", () => {
    expect(DockviewReact).toBeDefined();
    expect(themeLight.name).toBeTypeOf("string");
    expect(themeLight.className).toBeTypeOf("string");
    const _t: DockviewTheme = { ...themeLight, name: "isol8" };
    expect(_t.name).toBe("isol8");
    // Type-only names — reference them so tsc checks they exist:
    const _u:
      | DockviewReadyEvent
      | DockviewApi
      | IDockviewPanelProps
      | SerializedDockview
      | AddPanelOptions
      | null = null;
    expect(_u).toBeNull();
  });
});
