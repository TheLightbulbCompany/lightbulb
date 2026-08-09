import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";

// Mock sonner so we can assert on toast calls without mounting a DOM Toaster.
const toastError = vi.fn();
const toastSuccess = vi.fn();
const toastDefault = vi.fn();
const toastLoading = vi.fn();
const toastDismiss = vi.fn();
const toastInfo = vi.fn();
vi.mock("sonner", () => ({
  toast: Object.assign((...args: unknown[]) => toastDefault(...args), {
    error: (...args: unknown[]) => toastError(...args),
    success: (...args: unknown[]) => toastSuccess(...args),
    loading: (...args: unknown[]) => toastLoading(...args),
    dismiss: (...args: unknown[]) => toastDismiss(...args),
    info: (...args: unknown[]) => toastInfo(...args),
  }),
}));

import { ReportButton } from "@/components/support/ReportButton";
import { notify, notifySuccess, notifyError, notifyApiError, notifyLoading, notifyFailure, dismissNotification } from "../notify";
import { ApiError } from "../api";

/** The `opts` arg of the most recent toast.error call. */
function lastErrorOpts(): { duration?: number; action?: ReactElement } {
  return toastError.mock.calls.at(-1)![1];
}

beforeEach(() => {
  toastError.mockClear();
  toastSuccess.mockClear();
  toastDefault.mockClear();
  toastLoading.mockClear();
  toastDismiss.mockClear();
  toastInfo.mockClear();
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

describe("notifyApiError — free-tier gates convert instead of erroring", () => {
  const GATE_COPY = "You've used today's free usage. Resets at midnight UTC.";
  const gateError = (code = "trial_daily_cap") =>
    new ApiError(
      402,
      { detail: { blocked: true, code, message: GATE_COPY } },
      GATE_COPY,
      "req_402",
    );

  it("renders a sticky info toast with the gate copy and an Upgrade action", () => {
    notifyApiError(gateError(), "Failed to post comment");
    // Not a red Report-able error — the cap is the product working.
    expect(toastError).not.toHaveBeenCalled();
    const [msg, opts] = toastInfo.mock.calls.at(-1)!;
    expect(msg).toBe(GATE_COPY);
    expect(opts.duration).toBe(Infinity);
  });

  it("dedupes by gate code so repeated blocked clicks replace one toast", () => {
    notifyApiError(gateError());
    expect(toastInfo.mock.calls.at(-1)![1].id).toBe("gate:trial_daily_cap");
  });

  it("the Upgrade action is a client-side Link into the billing panel", () => {
    // A <Link>, not window.location — a hard navigation would tear down the
    // workspace shell at the moment the user is deciding whether to pay.
    notifyApiError(gateError());
    render(toastInfo.mock.calls.at(-1)![1].action as ReactElement);
    const link = screen.getByRole("link", { name: "Upgrade" });
    expect(link).toHaveAttribute("href", "/settings?panel=billing");
  });

  it("converts the routines subscription gate too", () => {
    notifyApiError(gateError("routines_require_subscription"));
    expect(toastInfo).toHaveBeenCalled();
    expect(toastError).not.toHaveBeenCalled();
  });

  it("leaves PAID blocked codes on the reportable error path — Upgrade is wrong copy for a paying owner", () => {
    notifyApiError(gateError("credits_required"));
    expect(toastInfo).not.toHaveBeenCalled();
    expect(toastError).toHaveBeenCalled();
    // The gate copy still surfaces as the message (via the client's
    // structured-detail promotion), just without the conversion framing.
    expect(toastError.mock.calls.at(-1)![0]).toBe(GATE_COPY);
  });

  it("leaves non-blocked ApiErrors on the reportable error path", () => {
    notifyApiError(new ApiError(402, { detail: "plain string detail" }, "plain string detail"));
    expect(toastInfo).not.toHaveBeenCalled();
    expect(toastError).toHaveBeenCalled();
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

describe("notifyLoading", () => {
  it("is persistent and keyed by dedupeKey", () => {
    notifyLoading("working…", { dedupeKey: "run-1" });
    const [msg, opts] = toastLoading.mock.calls.at(-1)!;
    expect(msg).toBe("working…");
    expect(opts).toMatchObject({ id: "run-1", duration: Infinity });
  });
});

describe("notifyFailure", () => {
  it("is sticky and forwards a custom action", () => {
    const onClick = vi.fn();
    notifyFailure("run failed", { dedupeKey: "run-1", action: { label: "View", onClick } });
    const [msg, opts] = toastError.mock.calls.at(-1)!;
    expect(msg).toBe("run failed");
    expect(opts).toMatchObject({ id: "run-1", duration: Infinity, action: { label: "View" } });
  });
});

describe("notifySuccess action passthrough", () => {
  it("forwards a custom action while keeping auto-dismiss", () => {
    const onClick = vi.fn();
    notifySuccess("run finished", { dedupeKey: "run-1", action: { label: "View", onClick } });
    const [, opts] = toastSuccess.mock.calls.at(-1)!;
    expect(opts).toMatchObject({ id: "run-1", action: { label: "View" } });
    expect(opts.duration).not.toBe(Infinity);
  });
});

describe("dismissNotification", () => {
  it("delegates to toast.dismiss with the key", () => {
    dismissNotification("run-1");
    expect(toastDismiss).toHaveBeenCalledWith("run-1");
  });
});
