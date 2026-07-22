import { renderHook } from "@testing-library/react-native";
import type { ReactNode } from "react";

import {
  ContainerStatusContext,
  useContainerStatusContext,
} from "../containerStatusContext";

// mobile onboarding-parity Slice 2: this context is the wiring between
// ProvisionGate's poll and FirstRunGate's funnel decision. Covered directly
// (in addition to the consuming components' own tests) since a mistake here
// — e.g. the wrong Context object, or a missing Provider wrap — would not
// be caught by FirstRunGate's tests, which mock this module's hook outright.
describe("useContainerStatusContext", () => {
  it("returns null outside any provider", async () => {
    const { result } = await renderHook(() => useContainerStatusContext());
    expect(result.current).toBeNull();
  });

  it("returns the value supplied by ContainerStatusContext.Provider", async () => {
    const value = { kind: "ready" as const, onboarded: false };
    const wrapper = ({ children }: { children: ReactNode }) => (
      <ContainerStatusContext.Provider value={value}>
        {children}
      </ContainerStatusContext.Provider>
    );
    const { result } = await renderHook(() => useContainerStatusContext(), {
      wrapper,
    });
    expect(result.current).toEqual({ kind: "ready", onboarded: false });
  });
});
