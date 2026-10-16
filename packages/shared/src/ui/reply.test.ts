import { describe, expect, it } from "vitest";
import { choiceReply, formReply, tableReply } from "./reply";

describe("card replies read as something the person would type", () => {
  it("joins picks with commas and trims", () => {
    expect(choiceReply(["Studio"])).toBe("Studio");
    expect(choiceReply([" Starter ", "Studio"])).toBe("Starter, Studio");
    expect(choiceReply([])).toBe("");
  });
  it("writes one Label: value line per field, skipping empty values", () => {
    expect(formReply([{ label: "Name", value: "Ada" }, { label: "Email", value: "" }, { label: "Start", value: "2026-11-01" }]))
      .toBe("Name: Ada\nStart: 2026-11-01");
  });
  it("writes a markdown table and escapes pipes", () => {
    expect(tableReply(["Plan", "Price"], [["Starter", "49"], ["Pro | Team", "149"]])).toBe(
      "| Plan | Price |\n| --- | --- |\n| Starter | 49 |\n| Pro \\| Team | 149 |",
    );
  });
});
