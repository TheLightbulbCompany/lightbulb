// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { plainSnippet, SearchSnippet, snippetParts, snippetText } from "../searchSnippet";

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

  it("reads as plain text: markdown syntax dropped, the match still marked (W13)", () => {
    expect(snippetText("[Invite someone](https://dev.thelightbulb.company/chat?panel=invite) to **\u0001chai\u0002** now"))
      .toBe("Invite someone to \u0001chai\u0002 now");
    expect(snippetText("## Menu\n- *pumpkin* latte\n> `apple` crumble")).toBe("Menu pumpkin latte apple crumble");
    expect(snippetText("see [the doc](https://dev.thelightbulb.company/ch")).toBe("see the doc"); // cut inside the url
    expect(snippetText("snake_case_name stays")).toBe("snake_case_name stays");
    const { container } = render(<SearchSnippet snippet={"**\u0001chai\u0002** bun"} />);
    expect(container.textContent).toBe("chai bun");
    expect(container.querySelector("mark")?.textContent).toBe("chai");
  });
});
