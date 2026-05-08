import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  PublishAgentModal,
  slugify,
  storefrontUrlForSlug,
} from "@/components/marketplace/PublishAgentModal";

// ---------------------------------------------------------------------------
// useApi() mock — re-bound per test to script the responses for each call.
// We expose the 4 verbs that the modal touches: get/post/put/del.
// ---------------------------------------------------------------------------

type ApiMock = {
  get: ReturnType<typeof vi.fn>;
  post: ReturnType<typeof vi.fn>;
  put: ReturnType<typeof vi.fn>;
  del: ReturnType<typeof vi.fn>;
};

let api: ApiMock;

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    BACKEND_URL: "http://localhost:8000/api/v1",
    useApi: () => api,
  };
});

beforeEach(() => {
  api = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
  };
});

afterEach(() => {
  vi.clearAllMocks();
});

const AGENT = {
  agent_id: "research-agent",
  name: "Research Agent",
  description_md: "Helps with deep research tasks.",
};

// ---------------------------------------------------------------------------

describe("slugify", () => {
  it("converts arbitrary names to kebab-case slugs", () => {
    expect(slugify("Research Agent")).toBe("research-agent");
    expect(slugify("My  Cool Bot!!")).toBe("my-cool-bot");
    expect(slugify("  -leading-trailing-  ")).toBe("leading-trailing");
    expect(slugify("ALLCAPS")).toBe("allcaps");
  });
});

describe("PublishAgentModal — initial render", () => {
  it("renders the form immediately with auto-filled slug + name + description", async () => {
    render(<PublishAgentModal agent={AGENT} open={true} onClose={() => {}} />);

    const slugInput = (await screen.findByLabelText(/slug/i)) as HTMLInputElement;
    expect(slugInput.value).toBe("research-agent");

    const nameInput = screen.getByLabelText(/^Listing name$/i) as HTMLInputElement;
    expect(nameInput.value).toBe("Research Agent");

    const descInput = screen.getByLabelText(/storefront description/i) as HTMLTextAreaElement;
    expect(descInput.value).toBe("Helps with deep research tasks.");

    // No eligibility prefetch — the tier gate was retired in the flat-fee cutover.
    expect(api.get).not.toHaveBeenCalled();
  });
});

describe("PublishAgentModal — submit flow", () => {
  it("happy path: create + artifact + submit shows success state", async () => {
    api.post
      .mockResolvedValueOnce({ listing_id: "list_123", slug: "research-agent" })
      .mockResolvedValueOnce({
        listing_id: "list_123",
        version: 1,
        manifest_sha256: "sha",
        file_count: 4,
        bytes: 1024,
      })
      .mockResolvedValueOnce({ listing_id: "list_123", status: "review" });

    const onPublished = vi.fn();
    render(
      <PublishAgentModal
        agent={AGENT}
        open={true}
        onClose={() => {}}
        onPublished={onPublished}
      />,
    );
    await screen.findByLabelText(/slug/i);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^Publish$/i }));

    await waitFor(() =>
      expect(screen.getByText(/submitted for review/i)).toBeInTheDocument(),
    );

    // Storefront URL on success uses the dev fallback because the module-mock
    // pins BACKEND_URL to localhost. Production/dev hosts are covered by the
    // dedicated `storefrontUrlForSlug` describe block below.
    const storefrontLink = screen.getByRole("link", {
      name: /view storefront url/i,
    }) as HTMLAnchorElement;
    expect(storefrontLink.href).toBe("http://localhost:3000/marketplace/listing/research-agent");

    expect(api.post).toHaveBeenCalledTimes(3);
    expect(api.post).toHaveBeenNthCalledWith(
      1,
      "/marketplace/listings",
      expect.objectContaining({
        slug: "research-agent",
        name: "Research Agent",
        format: "openclaw",
        price_cents: 0,
        tags: [],
      }),
    );
    expect(api.post).toHaveBeenNthCalledWith(
      2,
      "/marketplace/listings/list_123/artifact-from-agent",
      { agent_id: "research-agent" },
    );
    expect(api.post).toHaveBeenNthCalledWith(
      3,
      "/marketplace/listings/list_123/submit",
      {},
    );
    expect(onPublished).toHaveBeenCalledWith("list_123");
  });

  it("step-1 slug collision (409) surfaces friendly error and stays on form", async () => {
    api.post.mockRejectedValueOnce(
      Object.assign(new Error("conflict"), {
        status: 409,
        detail: "slug already taken",
      }),
    );

    render(<PublishAgentModal agent={AGENT} open={true} onClose={() => {}} />);
    await screen.findByLabelText(/slug/i);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^Publish$/i }));

    // Friendly slug-collision message visible (the modal maps 409 → this copy)
    await waitFor(() =>
      expect(screen.getByText(/that slug is already taken/i)).toBeInTheDocument(),
    );

    // Stayed on form (not the generic error step) so the seller can edit slug
    // and retry — the form fields remain interactive.
    const slug = screen.getByLabelText(/slug/i) as HTMLInputElement;
    expect(slug).not.toBeDisabled();

    // Step 2 (artifact-from-agent) was NOT called — short-circuited at step 1.
    expect(api.post).toHaveBeenCalledTimes(1);
    expect(api.post).toHaveBeenCalledWith(
      "/marketplace/listings",
      expect.objectContaining({ slug: "research-agent" }),
    );
  });

  it("retry resumes from draft listing — step-1 not re-called after step-2 failure", async () => {
    // POST sequence:
    //   #1  /listings                          → success (creates draft)
    //   #2  /artifact-from-agent (first try)   → fail
    //   #3  /artifact-from-agent (retry)       → success
    //   #4  /submit                            → success
    api.post
      .mockResolvedValueOnce({ listing_id: "list_456", slug: "research-agent" })
      .mockRejectedValueOnce(
        Object.assign(new Error("snapshot transient"), {
          status: 500,
          detail: "snapshot failed",
        }),
      )
      .mockResolvedValueOnce({
        listing_id: "list_456",
        version: 1,
        manifest_sha256: "sha",
        file_count: 4,
        bytes: 1024,
      })
      .mockResolvedValueOnce({ listing_id: "list_456", status: "review" });

    const onPublished = vi.fn();
    render(
      <PublishAgentModal
        agent={AGENT}
        open={true}
        onClose={() => {}}
        onPublished={onPublished}
      />,
    );
    await screen.findByLabelText(/slug/i);

    const user = userEvent.setup();
    // First click: step 1 succeeds, step 2 fails, error state shown.
    await user.click(screen.getByRole("button", { name: /^Publish$/i }));
    await waitFor(() =>
      expect(screen.getByText(/snapshot failed/i)).toBeInTheDocument(),
    );
    // Button now reads "Retry" since we're in step="error".
    const retryBtn = await screen.findByRole("button", { name: /^Retry$/i });

    // Click retry: should NOT re-create the listing (skip step 1), retry step 2,
    // then run step 3.
    await user.click(retryBtn);

    await waitFor(() =>
      expect(screen.getByText(/submitted for review/i)).toBeInTheDocument(),
    );

    // /listings called exactly once across the whole flow.
    const listingsCalls = api.post.mock.calls.filter(
      (c) => c[0] === "/marketplace/listings",
    );
    expect(listingsCalls).toHaveLength(1);

    // /artifact-from-agent called twice (failed + succeeded).
    const artifactCalls = api.post.mock.calls.filter(
      (c) => c[0] === "/marketplace/listings/list_456/artifact-from-agent",
    );
    expect(artifactCalls).toHaveLength(2);

    // /submit called once.
    const submitCalls = api.post.mock.calls.filter(
      (c) => c[0] === "/marketplace/listings/list_456/submit",
    );
    expect(submitCalls).toHaveLength(1);

    expect(onPublished).toHaveBeenCalledWith("list_456");
  });
});

describe("storefrontUrlForSlug — host pattern", () => {
  // Storefront is path-based on the same isol8.co frontend (no separate
  // marketplace.* subdomain). Test pins the env→host mapping so the
  // publish-success "your listing is live at…" link goes to the right
  // dev/staging/prod surface.

  it("localhost backend → storefront dev port (uses module-mocked BACKEND_URL)", () => {
    expect(storefrontUrlForSlug("my-agent")).toBe(
      "http://localhost:3000/marketplace/listing/my-agent",
    );
  });

  it("api.isol8.co → isol8.co", async () => {
    vi.resetModules();
    vi.doMock("@/lib/api", async () => {
      const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
      return { ...actual, BACKEND_URL: "https://api.isol8.co/api/v1", useApi: () => api };
    });
    const mod = await import("@/components/marketplace/PublishAgentModal");
    expect(mod.storefrontUrlForSlug("my-agent")).toBe(
      "https://isol8.co/marketplace/listing/my-agent",
    );
    vi.doUnmock("@/lib/api");
  });

  it("api-dev.isol8.co → dev.isol8.co", async () => {
    vi.resetModules();
    vi.doMock("@/lib/api", async () => {
      const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
      return {
        ...actual,
        BACKEND_URL: "https://api-dev.isol8.co/api/v1",
        useApi: () => api,
      };
    });
    const mod = await import("@/components/marketplace/PublishAgentModal");
    expect(mod.storefrontUrlForSlug("my-agent")).toBe(
      "https://dev.isol8.co/marketplace/listing/my-agent",
    );
    vi.doUnmock("@/lib/api");
  });

  it("api-staging.isol8.co → staging.isol8.co", async () => {
    vi.resetModules();
    vi.doMock("@/lib/api", async () => {
      const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
      return {
        ...actual,
        BACKEND_URL: "https://api-staging.isol8.co/api/v1",
        useApi: () => api,
      };
    });
    const mod = await import("@/components/marketplace/PublishAgentModal");
    expect(mod.storefrontUrlForSlug("my-agent")).toBe(
      "https://staging.isol8.co/marketplace/listing/my-agent",
    );
    vi.doUnmock("@/lib/api");
  });

  it("encodes special characters in slug", () => {
    // The slug validator forbids these in real usage, but the function should
    // still safely encode whatever is passed in.
    expect(storefrontUrlForSlug("a b/c")).toBe(
      "http://localhost:3000/marketplace/listing/a%20b%2Fc",
    );
  });
});

describe("PublishAgentModal — slug validation", () => {
  it("invalid slug surfaces inline error and prevents submit", async () => {
    render(<PublishAgentModal agent={AGENT} open={true} onClose={() => {}} />);
    const slug = (await screen.findByLabelText(/slug/i)) as HTMLInputElement;

    const user = userEvent.setup();
    await user.clear(slug);
    await user.type(slug, "Invalid Slug!");
    // Trigger blur to reveal the error
    await user.tab();

    expect(
      screen.getByText(/lowercase letters, digits, and hyphens/i),
    ).toBeInTheDocument();

    // Submit click does not advance
    await user.click(screen.getByRole("button", { name: /^Publish$/i }));
    expect(api.post).not.toHaveBeenCalled();
  });
});
