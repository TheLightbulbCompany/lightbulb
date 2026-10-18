import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { DocumentContents } from "../DocumentContents";

it("a repeated heading scrolls to its own occurrence, not the first", () => {
  const body = document.createElement("div");
  body.innerHTML = "<h2>Notes</h2><p>a</p><h2>Other</h2><h2>Notes</h2><p>b</p>";
  const [first, , second] = Array.from(body.querySelectorAll("h2"));
  first.scrollIntoView = vi.fn();
  second.scrollIntoView = vi.fn();
  const entries = [{ level: 2 as const, text: "Notes" }, { level: 2 as const, text: "Other" }, { level: 2 as const, text: "Notes" }];
  render(<DocumentContents entries={entries} bodyRef={{ current: body }} />);
  fireEvent.click(screen.getAllByRole("button", { name: "Notes" })[1]);
  expect(second.scrollIntoView).toHaveBeenCalledTimes(1);
  expect(first.scrollIntoView).not.toHaveBeenCalled();
});
