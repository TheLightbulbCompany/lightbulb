import { describe, expect, it, vi, beforeEach } from "vitest";
import { searchListings, myListings } from "@/lib/marketplace/api";

const fetchSpy = vi.fn();
vi.stubGlobal("fetch", fetchSpy);

describe("marketplace api", () => {
  beforeEach(() => fetchSpy.mockReset());

  it("searchListings sends q + category + sort", async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ items: [] }),
    });
    await searchListings({ q: "atlas", category: "engineering", sort: "recent" });
    const url = (fetchSpy.mock.calls[0][0] as string);
    expect(url).toMatch(/q=atlas/);
    expect(url).toMatch(/category=engineering/);
    expect(url).toMatch(/sort=recent/);
    expect(url).toMatch(/format=openclaw/);
  });

  it("myListings includes seller_id and status_in with auth header", async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ items: [] }),
    });
    await myListings({ sellerId: "u_me", jwt: "tok_abc" });
    const url = (fetchSpy.mock.calls[0][0] as string);
    expect(url).toMatch(/seller_id=u_me/);
    expect(url).toMatch(/status_in=draft%2Creview%2Cpublished%2Cretired%2Ctaken_down/);
    const headers = (fetchSpy.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer tok_abc");
  });
});
