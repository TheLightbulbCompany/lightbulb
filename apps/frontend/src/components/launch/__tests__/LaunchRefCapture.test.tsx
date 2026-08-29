import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const params = new URLSearchParams();
vi.mock("next/navigation", () => ({ useSearchParams: () => params }));

import { LAUNCH_REF_KEY } from "@/lib/launch";
import { LaunchRefCapture } from "../LaunchRefCapture";

describe("LaunchRefCapture", () => {
  beforeEach(() => {
    localStorage.clear();
    params.delete("ref");
  });

  it("stores ?ref= from the landing root into localStorage", () => {
    params.set("ref", "abc23456");
    render(<LaunchRefCapture />);
    expect(localStorage.getItem(LAUNCH_REF_KEY)).toBe("ABC23456");
  });

  it("renders nothing and leaves a previously stored code alone", () => {
    localStorage.setItem(LAUNCH_REF_KEY, "K7QX2MPP");
    const { container } = render(<LaunchRefCapture />);
    expect(container).toBeEmptyDOMElement();
    expect(localStorage.getItem(LAUNCH_REF_KEY)).toBe("K7QX2MPP");
  });
});
