import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));

import SecurityPage, { metadata } from "@/app/security/page";

describe("/security", () => {
  it("names the three provider paths and links the Privacy Policy", () => {
    const { container } = render(<SecurityPage />);
    const text = container.textContent ?? "";
    for (const path of ["Lightbulb-managed", "Your ChatGPT account", "Your own key"]) {
      expect(text).toContain(path);
    }
    expect(text).toContain("we do not use it to train AI models");
    expect(screen.getByRole("link", { name: "Privacy Policy" })).toHaveAttribute("href", "/privacy");
    expect(metadata.alternates?.canonical).toBe("https://thelightbulb.company/security");
  });
});
