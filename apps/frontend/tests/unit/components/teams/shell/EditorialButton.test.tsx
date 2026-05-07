import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EditorialButton } from "@/components/teams/shell/EditorialButton";

describe("EditorialButton", () => {
  it("renders children", () => {
    render(<EditorialButton>Click me</EditorialButton>);
    expect(screen.getByRole("button", { name: "Click me" })).toBeInTheDocument();
  });

  it("applies primary variant by default", () => {
    render(<EditorialButton>X</EditorialButton>);
    expect(screen.getByRole("button")).toHaveClass("editorial-btn", "editorial-btn--primary");
  });

  it.each(["primary", "secondary", "ghost", "destructive"] as const)(
    "applies %s variant class",
    (variant) => {
      render(<EditorialButton variant={variant}>X</EditorialButton>);
      expect(screen.getByRole("button")).toHaveClass(`editorial-btn--${variant}`);
    },
  );

  it.each(["sm", "md", "lg", "icon"] as const)("applies %s size class", (size) => {
    render(<EditorialButton size={size}>X</EditorialButton>);
    expect(screen.getByRole("button")).toHaveClass(`editorial-btn--${size}`);
  });

  it("merges caller className", () => {
    render(<EditorialButton className="custom-class">X</EditorialButton>);
    expect(screen.getByRole("button")).toHaveClass("custom-class", "editorial-btn", "editorial-btn--primary");
  });

  it("forwards onClick", async () => {
    const onClick = vi.fn();
    render(<EditorialButton onClick={onClick}>X</EditorialButton>);
    await userEvent.click(screen.getByRole("button"));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("respects disabled", async () => {
    const onClick = vi.fn();
    render(<EditorialButton onClick={onClick} disabled>X</EditorialButton>);
    await userEvent.click(screen.getByRole("button"));
    expect(onClick).not.toHaveBeenCalled();
    expect(screen.getByRole("button")).toBeDisabled();
  });

  it("forwards ref", () => {
    const ref = { current: null as HTMLButtonElement | null };
    render(<EditorialButton ref={ref}>X</EditorialButton>);
    expect(ref.current).toBeInstanceOf(HTMLButtonElement);
    expect(ref.current).toBe(screen.getByRole("button"));
  });
});
