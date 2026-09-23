/**
 * Shares the SAME `/container/status` read `ProvisionGate` already holds
 * (`useContainerStatus`, `containerStatus.ts`) with descendants, so a nested
 * gate never opens a second reader for data the parent already has (mobile
 * onboarding-parity Slice 2 — this replaces FirstRunGate's old standalone
 * `/billing/account` probe).
 *
 * Deliberately split out of `containerStatus.ts` into its own file: this
 * module's only runtime dependency is `react` (the `ContainerStatus` import
 * below is `import type`, erased at build time), so a consumer that only
 * needs the context — not the status hook and everything it pulls in
 * (`@lightbulb/shared`, the platform adapter, Clerk) — doesn't pull those in
 * too, and a test that mocks `containerStatus.ts` wholesale to fake the read
 * doesn't also have to fake this.
 */
import { createContext, useContext } from "react";

import type { ContainerStatus } from "./containerStatus";

export const ContainerStatusContext = createContext<ContainerStatus | null>(
  null,
);

/**
 * Reads the status `ProvisionGate` provides via `ContainerStatusContext`.
 * Returns `null` when rendered outside a provider (or before one exists in
 * a test) — callers must treat `null` exactly like any other "not yet
 * known" status: don't show, don't block.
 */
export function useContainerStatusContext(): ContainerStatus | null {
  return useContext(ContainerStatusContext);
}
