import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useScrollToHashAnchor } from "../useScrollToHashAnchor";

function Anchored({ ready }: { ready: boolean }) {
  useScrollToHashAnchor(ready);
  return <div id="comment-42">target</div>;
}

describe("useScrollToHashAnchor", () => {
  it("scrolls the element matching the current hash once ready", () => {
    window.location.hash = "#comment-42";
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    render(<Anchored ready />);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "start", behavior: "smooth" });
    window.location.hash = "";
  });

  it("does nothing while not ready", () => {
    window.location.hash = "#comment-42";
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    render(<Anchored ready={false} />);
    expect(scrollIntoView).not.toHaveBeenCalled();
    window.location.hash = "";
  });

  it("is a no-op with no hash", () => {
    window.location.hash = "";
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    render(<Anchored ready />);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});
