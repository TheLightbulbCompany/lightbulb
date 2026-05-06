import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";

import { useCatalog } from "@/hooks/useCatalog";

const mockGet = vi.fn();
const mockPost = vi.fn();

vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: mockGet, post: mockPost }),
}));

function wrapper({ children }: { children: ReactNode }) {
  return <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>;
}

const FEATURED = "/marketplace/listings/featured";
const INSTALLED = "/marketplace/me/installed-templates";

describe("useCatalog", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
  });

  it("maps marketplace listings into the CatalogAgent shape", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === FEATURED) {
        return Promise.resolve({
          items: [
            {
              slug: "pitch",
              name: "Pitch",
              version: 3,
              description_md: "Sales",
              manifest_json: {
                emoji: "🎯",
                vibe: "Direct",
                suggested_model: "qwen",
              },
            },
          ],
        });
      }
      if (path === INSTALLED) return Promise.resolve({ installed: [] });
      throw new Error(`Unexpected GET ${path}`);
    });

    const { result } = renderHook(() => useCatalog(), { wrapper });
    await waitFor(() => expect(result.current.agents.length).toBe(1));
    const agent = result.current.agents[0];
    expect(agent.slug).toBe("pitch");
    expect(agent.name).toBe("Pitch");
    expect(agent.emoji).toBe("🎯");
    expect(agent.vibe).toBe("Direct");
    expect(agent.suggested_model).toBe("qwen");
    expect(agent.description).toBe("Sales");
  });

  it("filters out templates already installed", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === FEATURED) {
        return Promise.resolve({
          items: [
            { slug: "pitch", name: "Pitch", version: 3, description_md: "" },
            { slug: "echo", name: "Echo", version: 1, description_md: "" },
          ],
        });
      }
      if (path === INSTALLED) {
        return Promise.resolve({
          installed: [{ agent_id: "agent_1", template_slug: "pitch", template_version: 3 }],
        });
      }
      throw new Error();
    });

    const { result } = renderHook(() => useCatalog(), { wrapper });
    await waitFor(() => expect(result.current.agents.length).toBe(1));
    expect(result.current.agents[0].slug).toBe("echo");
  });

  it("deploy() POSTs the marketplace deploy URL and revalidates", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === FEATURED) return Promise.resolve({ items: [] });
      if (path === INSTALLED) return Promise.resolve({ installed: [] });
      throw new Error();
    });
    mockPost.mockResolvedValue({
      agent_id: "agent_new",
      slug: "pitch",
      version: 3,
      skills_added: [],
    });

    const { result } = renderHook(() => useCatalog(), { wrapper });
    await waitFor(() => expect(result.current.agents).toBeDefined());

    let deployResult: unknown;
    await act(async () => {
      deployResult = await result.current.deploy("pitch");
    });
    expect(mockPost).toHaveBeenCalledWith("/marketplace/listings/pitch/deploy", {});
    expect(deployResult).toMatchObject({ agent_id: "agent_new" });
  });

  it("URL-encodes the slug on deploy", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === FEATURED) return Promise.resolve({ items: [] });
      if (path === INSTALLED) return Promise.resolve({ installed: [] });
      throw new Error();
    });
    mockPost.mockResolvedValue({ agent_id: "agent_new", slug: "weird/slug", version: 1 });

    const { result } = renderHook(() => useCatalog(), { wrapper });
    await waitFor(() => expect(result.current.agents).toBeDefined());

    await act(async () => {
      await result.current.deploy("weird/slug");
    });
    expect(mockPost).toHaveBeenCalledWith(
      "/marketplace/listings/weird%2Fslug/deploy",
      {},
    );
  });
});
