// AgentSpendSection's contract is HONESTY: every figure it prints must be one
// it can source, and anything missing or unverified must say so rather than
// render a confident zero. These pin that, plus the section chrome and the
// never-run-an-agent empty state.
//
// Mocking convention: mock useBilling/clerk, and mock swr by key so this
// section reads the same fixture shape /billing/usage returns.
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const usageFixture = {
  period: "2026-08",
  total_spend: 12.3456,
  total_input_tokens: 1000,
  total_output_tokens: 2000,
  total_cache_read_tokens: 0,
  total_cache_write_tokens: 0,
  request_count: 42,
  lifetime_spend: 99.5,
  by_member: [],
};

const state: {
  account: Record<string, unknown> | null;
  usage: typeof usageFixture | null;
  usageLoading: boolean;
} = {
  account: { subscription_status: "active", provider_choice: "openrouter_managed" },
  usage: usageFixture,
  usageLoading: false,
};

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: state.account,
    isLoading: false,
    fetchUsage: async () => state.usage,
    refresh: () => {},
  }),
}));
vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ membership: null }) }));
vi.mock("swr", () => ({
  default: (key: string) => {
    if (key === "/billing/usage") {
      return {
        data: state.usage,
        error: undefined,
        isLoading: state.usageLoading,
        mutate: vi.fn(),
      };
    }
    return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
  },
}));

import { AgentSpendSection } from "../AgentSpendSection";

function setup(over: Partial<typeof state> = {}) {
  Object.assign(state, {
    account: { subscription_status: "active", provider_choice: "openrouter_managed" },
    usage: usageFixture,
    usageLoading: false,
    ...over,
  });
  return render(<AgentSpendSection />);
}

describe("AgentSpendSection chrome", () => {
  it("is a section labelled Agent spend that names the period and links to Model & Credits", () => {
    setup();
    const section = screen.getByRole("region", { name: "Agent spend" });
    expect(within(section).getByText("August")).toBeInTheDocument(); // the month, not the raw "2026-08"
    expect(within(section).getByRole("link", { name: "Manage credits" })).toHaveAttribute("href", "/chat?panel=llm");
    expect(within(section).queryByText(/Cache/)).toBeNull(); // the token grid does not come along
  });
});

describe("AgentSpendSection headline", () => {
  it("shows the live agent-run count beside the figure", () => {
    setup();
    const headline = screen.getByTestId("spend-headline");
    expect(headline).toHaveTextContent("Agent runs");
    expect(headline).toHaveTextContent("42");
  });

  it("oauth: says the plan covers it instead of inventing a dollar amount", () => {
    setup({ account: { subscription_status: "active", provider_choice: "chatgpt_oauth" } });
    expect(screen.getByTestId("spend-headline")).toHaveTextContent("Not billed by us");
    expect(screen.getByText(/run on your ChatGPT plan/i)).toBeInTheDocument();
    expect(screen.queryByText("Partial")).toBeNull();
  });

  it("never prints the analytics-only usage-counter dollars", () => {
    setup();
    // /billing/usage's total_spend (12.3456) and lifetime_spend (99.5) are DDB
    // counters, not a billed figure — #1236 removed them from the UI and this
    // tab must not sneak them back in under the name "spend".
    expect(screen.queryByText(/12\.3456/)).toBeNull();
    expect(screen.queryByText(/99\.5/)).toBeNull();
  });

  it("labels an unknown provider Partial and admits it cannot tell which case it is", () => {
    setup({ account: { subscription_status: "active", provider_choice: null } });
    expect(screen.getByTestId("spend-headline")).toHaveTextContent("Not shown");
    expect(screen.getByText("Partial")).toBeInTheDocument();
  });

  it("BYO key: states the charges are on the provider's invoice, not Partial", () => {
    setup({ account: { subscription_status: "active", provider_choice: "byo_key" } });
    expect(screen.getByTestId("spend-headline")).toHaveTextContent("Not billed by us");
    expect(screen.getByText(/we cannot see them/i)).toBeInTheDocument();
    expect(screen.queryByText("Partial")).toBeNull();
  });

  it("free tier: 'No charge' is a fact, so it carries no Partial chip", () => {
    setup({ account: { subscription_status: "trial_local", provider_choice: null } });
    expect(screen.getByTestId("spend-headline")).toHaveTextContent("No charge");
    expect(screen.queryByText("Partial")).toBeNull();
  });

  it("renders no headline until the account and usage have loaded", () => {
    setup({ usageLoading: true, usage: null });
    expect(screen.queryByTestId("spend-headline")).toBeNull();
  });
});

describe("AgentSpendSection empty state", () => {
  it("an owner who has never run an agent gets a true statement, not an error", () => {
    // lifetime_spend 0 as well as request_count 0 — the genuine first-run
    // owner, not a company sitting on a freshly rolled-over calendar month.
    setup({ usage: { ...usageFixture, request_count: 0, lifetime_spend: 0 } });
    expect(screen.getByText(/No agent runs this period\./)).toBeInTheDocument();
    expect(screen.getByText(/nothing has run yet/i)).toBeInTheDocument();
    // The all-zero token grid would be noise, so the body stands down too.
    expect(screen.queryByText("Cache Write")).toBeNull();
  });

  it("a 404 (no usage row at all) lands on the same empty state", () => {
    setup({ usage: null });
    expect(screen.getByText(/No agent runs this period\./)).toBeInTheDocument();
  });

  it("does not flash the empty state while usage is still loading", () => {
    setup({ usage: null, usageLoading: true });
    expect(screen.queryByText(/No agent runs this period\./)).toBeNull();
  });
});

// A calendar-month period resets to zero at 00:00 UTC on the 1st, so for the
// first days of every month an active company's Money tab read "No agent runs
// this period. Nothing has been billed because nothing has run yet." — word for
// word the copy a genuinely idle fleet gets. Seen live on 2026-09-01 by an owner
// who had spent ~$1.40 across 50+ agent runs the day before.
describe("AgentSpendSection empty state — month boundary vs a genuinely idle fleet", () => {
  const boundary = {
    usage: { ...usageFixture, period: "2026-09", request_count: 0, lifetime_spend: 1.4 },
  };

  it("names the period reset instead of claiming nothing has ever run", () => {
    setup(boundary);
    expect(screen.getByText(/No agent runs yet in September\./)).toBeInTheDocument();
    expect(screen.queryByText(/No agent runs this period\./)).toBeNull();
    expect(screen.queryByText(/nothing has run yet/i)).toBeNull();
    expect(screen.getByText(/ran in an earlier period/i)).toBeInTheDocument();
  });

  it("prints no dollar figure — there is no prior-period amount to source", () => {
    setup(boundary);
    // lifetime_spend is read as a boolean and nothing else. #1236 pulled the
    // DDB analytics counters out of this tab; naming the boundary must not
    // smuggle one back in as "last month you spent…".
    expect(screen.queryByText(/\$/)).toBeNull();
    expect(screen.queryByText(/1\.4/)).toBeNull();
  });

  it("oauth owners get the same boundary copy and still no dollars", () => {
    // An owner who moved from managed inference to ChatGPT OAuth carries a
    // non-zero lifetime counter. They pay OpenAI directly and we cannot see
    // that invoice, so the copy names the reset without implying any figure.
    setup({ ...boundary, account: { subscription_status: "active", provider_choice: "chatgpt_oauth" } });
    expect(screen.getByText(/No agent runs yet in September\./)).toBeInTheDocument();
    expect(screen.queryByText(/\$/)).toBeNull();
  });

  it("a company that has never run anything keeps the first-run copy", () => {
    setup({ usage: { ...usageFixture, period: "2026-09", request_count: 0, lifetime_spend: 0 } });
    expect(screen.getByText(/No agent runs this period\./)).toBeInTheDocument();
    expect(screen.queryByText(/ran in an earlier period/i)).toBeNull();
  });

  it("falls back to the first-run copy when the period label is unreadable", () => {
    setup({ usage: { ...usageFixture, period: "", request_count: 0, lifetime_spend: 1.4 } });
    expect(screen.getByText(/No agent runs this period\./)).toBeInTheDocument();
  });
});
