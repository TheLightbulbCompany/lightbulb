import { createParser } from "@openuidev/lang-core";
import { describe, expect, it } from "vitest";
import { CARD_ROOT } from "./constants";
import { CARD_COMPONENTS, cardLibrary } from "./library";

const parse = (program: string) => createParser(cardLibrary.toJSONSchema(), CARD_ROOT).parse(program);

describe("card library", () => {
  it("has the twenty names, Stack first", () => {
    expect(CARD_COMPONENTS.map((c) => c.name)).toEqual([
      "Stack", "Text", "Callout", "Metric", "Table", "Col", "EditableTable", "BarChart", "LineChart", "Steps",
      "Sources", "Image", "Choice", "Buttons", "Button", "FollowUps", "Form", "Input", "Select", "DatePicker",
    ]);
  });

  it("parses a question card and a table card", () => {
    const result = parse([
      'root = Stack([q, t, m])',
      'q = Choice("Which plan first?", ["Starter", "Studio"])',
      't = Table([Col("Plan", ["Starter", "Studio"]), Col("Price", [49, 149])])',
      'm = Metric("MRR", "$4,200", "+12%")',
    ].join("\n"));
    expect(result.meta.errors).toEqual([]);
    expect(result.root?.typeName).toBe("Stack");
    const children = result.root?.props.children as { typeName: string; props: Record<string, unknown> }[];
    expect(children.map((c) => c.typeName)).toEqual(["Choice", "Table", "Metric"]);
    expect(children[0].props).toMatchObject({ question: "Which plan first?", options: ["Starter", "Studio"] });
  });

  it("drops an unknown component and keeps the rest", () => {
    const result = parse('root = Stack([a, b])\na = Text("hi")\nb = Nope("x")');
    expect(result.root?.typeName).toBe("Stack");
    expect(result.meta.errors.map((e) => e.code)).toContain("unknown-component");
  });

  it("every component signature appears in the prompt", () => {
    const prompt = cardLibrary.prompt({});
    for (const c of CARD_COMPONENTS) expect(prompt).toContain(`${c.name}(`);
  });
});
