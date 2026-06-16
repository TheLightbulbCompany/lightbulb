import { describe, it, expect, vi, beforeEach } from "vitest";
import type { ReactElement } from "react";

// Mock sonner so we can assert on toast calls without mounting a DOM Toaster.
const toastError = vi.fn();
const toastSuccess = vi.fn();
const toastDefault = vi.fn();
vi.mock("sonner", () => ({
  toast: Object.assign((...args: unknown[]) => toastDefault(...args), {
    error: (...args: unknown[]) => toastError(...args),
    success: (...args: unknown[]) => toastSuccess(...args),
  }),
}));

import { ReportButton } from "@/components/support/ReportButton";
import { notify, notifySuccess, notifyError, notifyApiError } from "../notify";
import { ApiError } from "../api";

/** The `opts` arg of the most recent toast.error call. */
function lastErrorOpts(): { duration?: number; action?: ReactElement } {
  return toastError.mock.calls.at(-1)![1];
}

beforeEach(() => {
  toastError.mockClear();
  toastSuccess.mockClear();
  toastDefault.mockClear();
});

describe("notifyError", () => {
  it("is sticky — error toasts must not auto-dismiss", () => {
    notifyError("boom");
    expect(toastError.mock.calls.at(-1)![0]).toBe("boom");
    expect(lastErrorOpts().duration).toBe(Infinity);
  });

  it("omits the Report action when neither requestId nor error is given", () => {
    notifyError("boom");
    expect(lastErrorOpts().action).toBeUndefined();
  });

  it("attaches a ReportButton action carrying the requestId", () => {
    notifyError("boom", { requestId: "req_123" });
    const action = lastErrorOpts().action!;
    expect(action.type).toBe(ReportButton);
    expect((action.props as { requestId?: string }).requestId).toBe("req_123");
  });

  it("attaches a ReportButton action when error details are present", () => {
    notifyError("boom", { error: { message: "x" } });
    expect(lastErrorOpts().action!.type).toBe(ReportButton);
  });
});

describe("notifyApiError", () => {
  it("extracts message + requestId from an ApiError", () => {
    notifyApiError(new ApiError(500, null, "kaboom", "req_999"));
    const action = lastErrorOpts().action!;
    expect(toastError.mock.calls.at(-1)![0]).toBe("kaboom");
    expect(action.type).toBe(ReportButton);
    expect((action.props as { requestId?: string }).requestId).toBe("req_999");
  });

  it("falls back to the message for non-Error values (not reportable)", () => {
    notifyApiError("nope", "fallback msg");
    expect(toastError.mock.calls.at(-1)![0]).toBe("fallback msg");
    expect(lastErrorOpts().action).toBeUndefined();
  });
});

describe("notify / notifySuccess", () => {
  it("auto-dismiss with a finite duration", () => {
    notify("hi");
    notifySuccess("ok");
    expect(toastDefault.mock.calls.at(-1)![1].duration).toBeLessThan(Infinity);
    expect(toastSuccess.mock.calls.at(-1)![1].duration).toBeLessThan(Infinity);
  });
});
