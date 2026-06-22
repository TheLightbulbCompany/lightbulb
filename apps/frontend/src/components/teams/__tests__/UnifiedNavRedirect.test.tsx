import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";
import { UnifiedNavRedirect } from "@/components/teams/UnifiedNavRedirect";

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock }),
}));

let unified = false;
vi.mock("@/hooks/useUnifiedNav", () => ({
  useUnifiedNav: () => unified,
}));

describe("UnifiedNavRedirect (Phase E, flag-gated)", () => {
  beforeEach(() => {
    replaceMock.mockClear();
    unified = false;
  });

  it("redirects to /chat when unified nav is enabled", () => {
    unified = true;
    render(<UnifiedNavRedirect />);
    expect(replaceMock).toHaveBeenCalledWith("/chat");
  });

  it("does NOT redirect when the flag is off (/teams renders untouched)", () => {
    unified = false;
    render(<UnifiedNavRedirect />);
    expect(replaceMock).not.toHaveBeenCalled();
  });
});
