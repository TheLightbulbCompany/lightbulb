import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ orgId: "org_1", userId: "u1", isLoaded: true }) }));
const get = vi.hoisted(() => vi.fn());
const put = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, put }) }));

import { SWRConfig } from "swr";
import { DEFAULT_CONFIG, useGoLiveAfterPayment } from "../data";

const wrapper = ({ children }: { children: React.ReactNode }) =>
  <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>;
const routes = (config: object, plan: string) => (path: string) =>
  Promise.resolve(path === "/voice/agent" ? { config, agents: [] } : { status: plan });

beforeEach(() => { get.mockReset(); put.mockReset().mockResolvedValue({}); });

it("goes live by itself once the plan it waited for is paid (prod 11-10)", async () => {
  get.mockImplementation(routes({ ...DEFAULT_CONFIG, onboardingStep: 5, goLiveRequested: true }, "active"));
  renderHook(() => useGoLiveAfterPayment(), { wrapper });
  await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
  const sent = put.mock.calls[0][1].config;
  expect(sent).toMatchObject({ live: true, paused: false });
  expect(sent.goLiveRequested).toBeUndefined();
  expect(sent.onboardingStep).toBeUndefined();
});

it("waits while the plan is unpaid, and never acts for an owner who did not press Go live", async () => {
  get.mockImplementation(routes({ ...DEFAULT_CONFIG, goLiveRequested: true }, "pending_payment"));
  const a = renderHook(() => useGoLiveAfterPayment(), { wrapper });
  await waitFor(() => expect(get).toHaveBeenCalledWith("/billing/voice-plan"));
  a.unmount();
  get.mockReset().mockImplementation(routes({ ...DEFAULT_CONFIG }, "active"));
  renderHook(() => useGoLiveAfterPayment(), { wrapper });
  await waitFor(() => expect(get).toHaveBeenCalledWith("/voice/agent"));
  expect(put).not.toHaveBeenCalled();
});
