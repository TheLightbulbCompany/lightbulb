import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Listing } from "@/lib/marketplace/types";

let auth = { isSignedIn: true, userId: "user_seller", orgId: "org_seller" as string | null };
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ ...auth, getToken: async () => "jwt" }),
  useUser: () => ({ user: null }),
}));

import { BuyButton } from "../BuyButton";

const paid = { listing_id: "lst_1", price_cents: 2000, seller_id: "user_seller" } as Listing;

describe("BuyButton on a paid listing", () => {
  it("offers the seller the free deploy, captioned, and no Buy", () => {
    render(<BuyButton listing={paid} />);
    expect(screen.getByText("This is your listing")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /deploy to my lightbulb container/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /buy for/i })).not.toBeInTheDocument();
  });

  it("does not offer Buy on a workspace-published listing to that workspace", () => {
    render(<BuyButton listing={{ ...paid, seller_id: "org_seller" }} />);
    expect(screen.getByText("This is your listing")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /buy for/i })).not.toBeInTheDocument();
  });

  it("offers Buy to anyone else", () => {
    auth = { isSignedIn: true, userId: "user_buyer", orgId: "org_buyer" };
    render(<BuyButton listing={paid} />);
    expect(screen.getByRole("button", { name: "Buy for $20.00" })).toBeInTheDocument();
    expect(screen.queryByText("This is your listing")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /deploy to my/i })).not.toBeInTheDocument();
  });
});
