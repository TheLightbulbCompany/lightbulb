import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import React from "react";

import { ConnectorIcon } from "../ConnectorIcons";
import { CONNECTOR_BRAND } from "../connectorBrand";
import { CONNECTOR_STEPS } from "@lightbulb/shared";

describe("ConnectorIcon", () => {
  it("draws a real mark for every connector card, so none renders bare", () => {
    for (const step of CONNECTOR_STEPS) {
      const { container } = render(<ConnectorIcon provider={step.id} />);
      const path = container.querySelector("path");
      expect(path, `${step.id} has no path`).not.toBeNull();
      // Simple Icons marks are long single paths; a stub would be a few chars.
      expect(path!.getAttribute("d")!.length).toBeGreaterThan(300);
    }
  });

  it("labels each mark for screen readers", () => {
    const { container } = render(<ConnectorIcon provider="stripe" />);
    expect(container.querySelector("title")?.textContent).toBe("Stripe");
  });

  it("renders nothing for a provider it has no mark for, rather than a broken icon", () => {
    const { container } = render(<ConnectorIcon provider={"invite" as never} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("CONNECTOR_BRAND", () => {
  it("keeps Stripe's real purple, which reads on both grounds", () => {
    expect(CONNECTOR_BRAND.stripe).toBe("#635BFF");
  });

  // The whole point: #181717 and #000000 vanish on the midnight ground. Same
  // fix providerBrand.ts already applies to the AWS wordmark.
  it.each(["github", "notion"] as const)(
    "draws the monochrome %s mark on the ground token, not a black literal",
    (provider) => {
      expect(CONNECTOR_BRAND[provider]).toBe("var(--foreground)");
    },
  );

  it("has a colour for every connector card", () => {
    for (const step of CONNECTOR_STEPS) {
      expect(CONNECTOR_BRAND[step.id as keyof typeof CONNECTOR_BRAND]).toBeTruthy();
    }
  });
});
