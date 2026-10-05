// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { plainSnippet, SearchSnippet, snippetParts } from "../searchSnippet";

describe("search snippets", () => {
  it("splits on the server's match marks", () => {
    expect(snippetParts("the \u0001deploy\u0002 fix")).toEqual([
      { text: "the ", match: false }, { text: "deploy", match: true }, { text: " fix", match: false },
    ]);
    expect(plainSnippet("the \u0001deploy\u0002 fix")).toBe("the deploy fix");
  });

  it("renders message HTML as text, never markup", () => {
    const { container } = render(<SearchSnippet snippet={"<img src=x onerror=alert(1)> \u0001hi\u0002"} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("mark")?.textContent).toBe("hi");
  });
});
