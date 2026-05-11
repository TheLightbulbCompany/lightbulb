// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { ChatErrorCard } from "../ChatErrorCard";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

describe("ChatErrorCard", () => {
  it("renders the rate_limit variant with a change-provider action", () => {
    render(<ChatErrorCard code="rate_limit" message="429 from upstream" />);
    expect(screen.getByText("Rate limit reached")).toBeInTheDocument();
    expect(screen.getByText("429 from upstream")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Change provider" });
    expect(link).toHaveAttribute("href", "/chat?panel=llm");
  });

  it("renders the context_length variant with a change-model action", () => {
    render(<ChatErrorCard code="context_length" message="" />);
    expect(screen.getByText("Conversation too long")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Change model" });
    expect(link).toHaveAttribute("href", "/chat?panel=llm");
  });

  it("renders the timeout variant with no action button", () => {
    render(<ChatErrorCard code="timeout" message="upstream timed out" />);
    expect(screen.getByText("Provider timeout")).toBeInTheDocument();
    expect(screen.getByText("upstream timed out")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("renders the refusal variant", () => {
    render(<ChatErrorCard code="refusal" message="" />);
    expect(screen.getByText("Request refused")).toBeInTheDocument();
  });

  it("falls back to a generic title for unknown codes and shows the upstream message verbatim", () => {
    render(<ChatErrorCard code="weird_new_code" message="some upstream detail" />);
    expect(screen.getByText("Failed to generate")).toBeInTheDocument();
    expect(screen.getByText("some upstream detail")).toBeInTheDocument();
  });

  it("falls back to a generic message when code is omitted", () => {
    render(<ChatErrorCard message="" />);
    expect(screen.getByText("Failed to generate")).toBeInTheDocument();
    expect(
      screen.getByText("Your agent encountered an error. Please try again."),
    ).toBeInTheDocument();
  });

  it("renders a retry button when onRetry is provided", () => {
    const onRetry = vi.fn();
    render(<ChatErrorCard code="rate_limit" message="rate limited" onRetry={onRetry} />);
    const retryBtn = screen.getByRole("button", { name: /retry/i });
    retryBtn.click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("attaches the code as a data attribute for analytics / e2e", () => {
    render(<ChatErrorCard code="rate_limit" message="m" />);
    expect(screen.getByTestId("chat-error-card")).toHaveAttribute(
      "data-error-code",
      "rate_limit",
    );
  });
});
