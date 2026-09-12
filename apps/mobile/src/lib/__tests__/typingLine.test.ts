import { typingLine } from "../typingLine";

describe("typingLine", () => {
  it("is empty when nobody is typing", () => {
    expect(typingLine([])).toBe("");
  });

  it("is singular for one typer", () => {
    expect(typingLine([{ displayName: "Alice" }])).toBe("Alice is typing…");
  });

  it("is plural for two or three typers, joined by commas", () => {
    expect(typingLine([{ displayName: "Alice" }, { displayName: "Bob" }])).toBe(
      "Alice, Bob are typing…",
    );
    expect(
      typingLine([{ displayName: "Alice" }, { displayName: "Bob" }, { displayName: "Carol" }]),
    ).toBe("Alice, Bob, Carol are typing…");
  });

  it("names only the first three and says 'and others' past that", () => {
    expect(
      typingLine([
        { displayName: "Alice" },
        { displayName: "Bob" },
        { displayName: "Carol" },
        { displayName: "Dave" },
      ]),
    ).toBe("Alice, Bob, Carol and others are typing…");
  });
});
