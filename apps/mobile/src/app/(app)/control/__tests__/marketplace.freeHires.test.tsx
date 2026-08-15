import { fireEvent, render, screen } from "@testing-library/react-native";
import { Alert } from "react-native";

// The phone hires FREE agents only. #1643 pulled the whole marketplace off iOS
// because a paid listing's "Get" handed a Stripe URL to the system browser;
// these pin the narrower shape that replaced it — free deploys work, and no
// affordance anywhere starts a purchase.

jest.mock("expo-router", () => ({
  router: { push: jest.fn(), back: jest.fn(), navigate: jest.fn() },
  Stack: { Screen: () => null },
  useLocalSearchParams: () => ({ id: "listing_1" }),
}));

let mockUserId: string | null = "user_me";
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: mockUserId, getToken: jest.fn(async () => "t") }),
}));

jest.mock("expo-crypto", () => ({
  randomUUID: () => "00000000-0000-4000-8000-000000000000",
}));

function makeListing(over: Record<string, unknown> = {}) {
  return {
    listing_id: "listing_1",
    slug: "jarvis",
    name: "Jarvis",
    description_md: "Runs errands.",
    format: "openclaw",
    price_cents: 0,
    tags: [],
    seller_id: "user_other",
    seller_display_name: "@someone",
    status: "published",
    version: 1,
    published_at: null,
    created_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

let mockSearchItems: ReturnType<typeof makeListing>[] = [];
let mockDetailListing = makeListing();
const mockDeploy = jest.fn(async () => ({ agent_uuid: "agent_new" }));
jest.mock("../../../../lib/marketplace", () => ({
  ...jest.requireActual("../../../../lib/marketplace"),
  useMarketplaceSearch: () => ({
    state: { kind: "ready", items: mockSearchItems },
    search: jest.fn(),
  }),
  useListingDetail: () => ({
    state: { kind: "ready", detail: { listing: mockDetailListing, manifest: null } },
    load: jest.fn(),
  }),
  useDeployListing: () => mockDeploy,
}));

import { router } from "expo-router";

import ControlIndex from "../index";
import ListingDetailScreen from "../listing/[id]";
import MarketplacePanel from "../marketplace";

beforeEach(() => {
  mockUserId = "user_me";
  mockSearchItems = [];
  mockDetailListing = makeListing();
  mockDeploy.mockClear();
  (router.push as jest.Mock).mockClear();
});

describe("control panel entry", () => {
  it("lists Marketplace and routes to it", async () => {
    await render(<ControlIndex />);
    fireEvent.press(screen.getByLabelText("Marketplace"));
    expect(router.push).toHaveBeenCalledWith("/(app)/control/marketplace");
  });
});

describe("marketplace search results", () => {
  it("hides paid listings and keeps free ones", async () => {
    mockSearchItems = [
      makeListing({ listing_id: "l_free", name: "Jarvis" }),
      makeListing({ listing_id: "l_paid", name: "Butler", price_cents: 1000 }),
    ];
    await render(<MarketplacePanel />);
    expect(screen.getByText("Jarvis")).toBeTruthy();
    expect(screen.queryByText("Butler")).toBeNull();
  });

  it("keeps the viewer's own paid listing — a seller deploys it for free", async () => {
    mockSearchItems = [
      makeListing({
        listing_id: "l_mine",
        name: "Butler",
        price_cents: 1000,
        seller_id: "user_me",
      }),
    ];
    await render(<MarketplacePanel />);
    expect(screen.getByText("Butler")).toBeTruthy();
  });
});

describe("listing detail", () => {
  it("deploys a free listing in one tap", async () => {
    const alert = jest.spyOn(Alert, "alert").mockImplementation(() => {});
    mockDetailListing = makeListing({ listing_id: "l_free" });
    await render(<ListingDetailScreen />);
    fireEvent.press(screen.getByLabelText("Deploy to my workspace"));
    expect(mockDeploy).toHaveBeenCalledWith("l_free");
    alert.mockRestore();
  });

  it("sends a paid listing to the web app, with nothing to tap", async () => {
    // Reachable by deep link even though search filters it out.
    mockDetailListing = makeListing({
      listing_id: "l_paid",
      name: "Butler",
      price_cents: 1000,
    });
    await render(<ListingDetailScreen />);
    expect(screen.getByText(/Paid agents are hired from the web app/)).toBeTruthy();
    expect(screen.queryByLabelText("Deploy to my workspace")).toBeNull();
    expect(screen.queryByText("Get")).toBeNull();
    expect(mockDeploy).not.toHaveBeenCalled();
  });

  it("offers the deploy on the seller's own paid listing", async () => {
    mockDetailListing = makeListing({
      listing_id: "l_mine",
      price_cents: 1000,
      seller_id: "user_me",
    });
    await render(<ListingDetailScreen />);
    expect(screen.getByLabelText("Deploy to my workspace")).toBeTruthy();
  });
});

describe("data layer", () => {
  it("exposes no checkout hook — a Stripe handoff is one import away", () => {
    const lib = jest.requireActual("../../../../lib/marketplace");
    expect(Object.keys(lib)).not.toContain("useCheckoutListing");
  });
});
