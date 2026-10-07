import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Tile } from "../Tile";

describe("Tile", () => {
  it("renders a plain div with the tone border when no href is given", () => {
    const { container } = render(<Tile title="isol8-dev-service" label="running" tone="ok" />);
    expect(container.querySelector("a")).toBeNull();
    expect(container.firstElementChild?.className).toContain("border-ok");
    expect(screen.getByText("running")).toBeTruthy();
  });

  it("renders the sub line truncated with a hover title", () => {
    render(<Tile title="Acme Corp" label="error" tone="error" sub="org_123abc" />);
    const sub = screen.getByText("org_123abc");
    expect(sub.className).toContain("truncate");
    expect(sub.getAttribute("title")).toBe("org_123abc");
  });

  it("renders an internal href as a same-tab link", () => {
    const { container } = render(
      <Tile title="Acme Corp" label="error" tone="error" href="/admin/users/org_123" />,
    );
    const a = container.querySelector("a");
    expect(a?.getAttribute("href")).toBe("/admin/users/org_123");
    expect(a?.getAttribute("target")).toBeNull();
  });

  it("renders an absolute href as a new-tab noreferrer anchor", () => {
    const { container } = render(
      <Tile
        title="ALB 5xx"
        label="critical"
        tone="error"
        href="https://us-east-1.console.aws.amazon.com/cloudwatch/home"
      />,
    );
    const a = container.querySelector("a");
    expect(a?.getAttribute("target")).toBe("_blank");
    expect(a?.getAttribute("rel")).toBe("noreferrer");
  });

  it("supports the warn tone", () => {
    const { container } = render(<Tile title="W28 unhealthy hosts" label="warning" tone="warn" />);
    expect(container.firstElementChild?.className).toContain("border-warn");
  });
});
