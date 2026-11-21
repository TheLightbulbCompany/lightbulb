// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { REF_KEY, RefCapture } from "../RefCapture";

const at = (query: string) => window.history.replaceState(null, "", `/os?${query}`);

beforeEach(() => localStorage.clear());

describe("RefCapture", () => {
  it("stores a valid ref, the latest click winning, and renders nothing", () => {
    localStorage.setItem(REF_KEY, "older");
    at("ref=abc123");
    const { container } = render(<RefCapture />);
    expect(localStorage.getItem(REF_KEY)).toBe("abc123");
    expect(container.innerHTML).toBe("");
  });

  it.each(["ref=", "ref=a.b", "ref=user_a", "ref=%3Cx%3E", `ref=${"x".repeat(65)}`])("%s: ignored", (query) => {
    at(query);
    render(<RefCapture />);
    expect(localStorage.getItem(REF_KEY)).toBeNull();
  });

  it("no ref writes nothing", () => {
    at("");
    render(<RefCapture />);
    expect(localStorage.length).toBe(0);
  });
});
