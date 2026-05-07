import { describe, it, expect } from "vitest";
import * as shell from "@/components/teams/shell";

describe("teams shell barrel", () => {
  it("exports all atoms", () => {
    // Components - some are forwardRef objects, some are functions
    expect(shell.EditorialButton).toBeDefined();
    expect(shell.EditorialInput).toBeDefined();
    expect(shell.EditorialSelect).toBeTypeOf("function");
    expect(shell.EditorialDialog).toBeTypeOf("function");
    expect(shell.EditorialCard).toBeTypeOf("function");
    expect(shell.PanelMasthead).toBeTypeOf("function");
    expect(shell.PanelToolbar).toBeTypeOf("function");
    expect(shell.PanelTabs).toBeTypeOf("function");
    expect(shell.PanelSection).toBeTypeOf("function");
    expect(shell.PanelEmptyState).toBeTypeOf("function");
    expect(shell.PanelDataTable).toBeTypeOf("function");
  });
});
