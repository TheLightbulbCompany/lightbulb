import { describe, test, expect } from "vitest";
import { render } from "@testing-library/react";
import { PageSkeleton } from "@/components/teams/shared/components/PageSkeleton";

describe("PageSkeleton", () => {
  test("renders the inbox variant by default", () => {
    const { container } = render(<PageSkeleton />);
    const rows = container.querySelectorAll("[data-skeleton-row]");
    expect(rows.length).toBeGreaterThanOrEqual(5);
  });

  test("rowCount prop overrides default", () => {
    const { container } = render(<PageSkeleton rowCount={3} />);
    const rows = container.querySelectorAll("[data-skeleton-row]");
    expect(rows.length).toBe(3);
  });

  test("uses animate-pulse for shimmer", () => {
    const { container } = render(<PageSkeleton />);
    expect(container.querySelector(".animate-pulse")).not.toBeNull();
  });
});
