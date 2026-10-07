// One door, either kind of trigger.
//
// Two defects this pins: wiring a connected app used to hand you a template
// title/description you could not edit (the fields were hidden on that rung),
// and "Add trigger" on an automation that already existed offered only a raw
// signed URL — no app picker, no provider wiring. Both surfaces now mount the
// SAME connected-app flow (shared/components/ConnectedAppTrigger.tsx).
//
// The parity case is the important one: an untouched draft must send NO
// title/description, so the registry template still renders server-side
// exactly as it did before this was editable.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { SWRResponse } from "swr";

type ReadStub = Partial<SWRResponse<unknown, Error>>;
let reads: Record<string, ReadStub> = {};
const post = vi.fn();
const patch = vi.fn();
const del = vi.fn();
const mutate = vi.fn();

vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ open: vi.fn() }),
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) =>
      reads[path] ?? { data: undefined, isLoading: false, error: undefined, mutate },
    post,
    patch,
    del,
  }),
}));

const apiPost = vi.fn();
const apiGet = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: apiGet, post: apiPost, put: vi.fn(), del: vi.fn() }),
}));

vi.mock("@/lib/notify", () => ({
  notifySuccess: vi.fn(),
  notifyApiError: vi.fn(),
  notifyConversionGate: () => null,
}));

let connections: Array<Record<string, unknown>> = [];
vi.mock("@/hooks/useConnections", () => ({
  useConnections: () => ({ connections }),
}));

import { RoutinesPanel } from "../RoutinesPanel";
import { RoutineDetailPanel } from "../RoutineDetailPanel";

// A guided-paste provider (Stripe's shape): no resource picker, so the form is
// app -> event -> draft.
const STRIPE = {
  provider: "stripe",
  display_name: "Stripe",
  status: "connected",
  inbound: {
    webhook_registration: "guided_paste",
    // Stripe names the fired event in the body (`type`), which is what lets
    // several events share ONE trigger.
    event_in_body: true,
    events: [
      { id: "invoice.payment_failed", label: "Invoice payment failed", starter_prompt: null },
      { id: "charge.refunded", label: "Charge refunded", starter_prompt: null },
      { id: "customer.subscription.deleted", label: "Subscription canceled", starter_prompt: null },
    ],
    template: {
      title: "Stripe: {event_label}",
      starter_prompt: "A Stripe webhook just fired: {event_label}. Work out what to do.",
    },
  },
};

// A shape whose setup writes the automation's own instructions — attaching it
// APPENDS a delimited block, which has to be stated before the owner confirms.
const GOOGLE = {
  provider: "google",
  display_name: "Google",
  status: "connected",
  inbound: {
    webhook_registration: "auto_google_watch",
    events: [{ id: "calendar.changed", label: "Calendar changed", starter_prompt: null }],
    template: { title: "Google: {event_label}", starter_prompt: "Something changed." },
  },
};

// A header-identified provider: Sentry names the fired event in
// Sentry-Hook-Resource, which never reaches the agent — so its events cannot
// share one trigger and the picker must stay single-select.
const SENTRY = {
  provider: "sentry",
  display_name: "Sentry",
  status: "connected",
  inbound: {
    webhook_registration: "guided_paste",
    // no event_in_body
    events: [
      { id: "issue", label: "Issue created or changed", starter_prompt: null },
      { id: "comment", label: "Comment", starter_prompt: null },
    ],
    template: {
      title: "Sentry: {event_label}",
      starter_prompt: "A Sentry webhook just fired: {event_label}.",
    },
  },
};

const STRIPE_RESULT = {
  routine_id: "rt_1",
  trigger_public_id: "pub_1",
  proxy_url: "https://api.example/api/v1/hooks/t/o1/pub_1",
  guided: true,
  instructions: "Paste it in Stripe.",
  secret_hint: "whsec_…",
};

beforeEach(() => {
  vi.clearAllMocks();
  connections = [];
  post.mockResolvedValue({});
  apiPost.mockResolvedValue(STRIPE_RESULT);
  apiGet.mockResolvedValue([]);
  reads = {
    "/routines": { data: { routines: [] }, isLoading: false, error: undefined, mutate },
    "/agents": { data: { agents: [] }, isLoading: false, error: undefined, mutate },
    "/routines/r1": {
      data: {
        id: "r1",
        title: "Revenue recovery",
        description: "Chase every failed invoice personally.",
        status: "active",
        triggers: [],
      },
      isLoading: false,
      error: undefined,
      mutate,
    },
    "/routines/r1/runs": { data: { runs: [] }, isLoading: false, error: undefined, mutate },
    "/routines/r1/revisions": {
      data: { revisions: [] },
      isLoading: false,
      error: undefined,
      mutate,
    },
  };
});

async function pickStripeInvoiceEvent(dialog: HTMLElement, user: ReturnType<typeof userEvent.setup>) {
  await user.click(within(dialog).getByRole("radio", { name: /a connected app/i }));
  await user.click(within(dialog).getByRole("combobox"));
  await user.click(await screen.findByRole("option", { name: "Stripe" }));
  await user.click(within(dialog).getByRole("button", { name: /select (an )?events?…/i }));
  await user.click(await screen.findByRole("option", { name: "Invoice payment failed" }));
}

/** Tick more events onto an already-started selection. The popover closes on
 * the FIRST pick (so single-select feels as it always did) and stays open
 * while a set is being built, so each extra event needs the list reopened. */
async function alsoPick(
  dialog: HTMLElement,
  user: ReturnType<typeof userEvent.setup>,
  labels: string[],
) {
  for (const label of labels) {
    await user.click(within(dialog).getByRole("button", { name: /invoice payment failed/i }));
    await user.click(await screen.findByRole("option", { name: label }));
    await user.keyboard("{Escape}");
  }
}

describe("New automation — the connected-app rung is writable", () => {
  it("pre-fills a draft title and description from the app and event", async () => {
    connections = [STRIPE];
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: /new automation/i }));
    const dialog = await screen.findByRole("dialog");
    await pickStripeInvoiceEvent(dialog, user);

    expect(within(dialog).getByLabelText("Title")).toHaveValue("Stripe: Invoice payment failed");
    expect(within(dialog).getByLabelText("Description")).toHaveValue(
      "A Stripe webhook just fired: Invoice payment failed. Work out what to do.",
    );
  });

  it("sends NO title/description when the draft is left untouched", async () => {
    connections = [STRIPE];
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: /new automation/i }));
    const dialog = await screen.findByRole("dialog");
    await pickStripeInvoiceEvent(dialog, user);
    await user.click(within(dialog).getByRole("button", { name: /^Create$/ }));

    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    const [path, body] = apiPost.mock.calls[0];
    expect(path).toBe("/automations/from-integration");
    // Byte-identical to the pre-editable 1-click body: the server's own
    // template renders the title and instructions.
    expect(body).toEqual({ provider: "stripe", event_ids: ["invoice.payment_failed"] });
  });

  it("sends what the owner typed over the draft", async () => {
    connections = [STRIPE];
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: /new automation/i }));
    const dialog = await screen.findByRole("dialog");
    await pickStripeInvoiceEvent(dialog, user);

    const description = within(dialog).getByLabelText("Description");
    await user.clear(description);
    await user.type(description, "Email the customer, then tell me.");
    const title = within(dialog).getByLabelText("Title");
    await user.clear(title);
    await user.type(title, "Dunning desk");
    await user.click(within(dialog).getByRole("button", { name: /^Create$/ }));

    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    expect(apiPost.mock.calls[0][1]).toEqual({
      provider: "stripe",
      event_ids: ["invoice.payment_failed"],
      title: "Dunning desk",
      description: "Email the customer, then tell me.",
    });
  });
});

describe("Add trigger — an existing automation takes either kind", () => {
  it("offers the same three-way choice the create dialog does", async () => {
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /add trigger/i }));
    const dialog = await screen.findByRole("dialog");

    expect(within(dialog).getByRole("radio", { name: /on a schedule/i })).toBeInTheDocument();
    expect(within(dialog).getByRole("radio", { name: /a connected app/i })).toBeInTheDocument();
    expect(within(dialog).getByRole("radio", { name: /any other app/i })).toBeInTheDocument();
  });

  it("arms a connected app ON this automation, without touching it", async () => {
    connections = [STRIPE];
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /add trigger/i }));
    const dialog = await screen.findByRole("dialog");
    await pickStripeInvoiceEvent(dialog, user);
    await user.click(within(dialog).getByRole("button", { name: /^Add$/ }));

    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    expect(apiPost.mock.calls[0]).toEqual([
      "/automations/from-integration",
      { provider: "stripe", event_ids: ["invoice.payment_failed"], routine_id: "r1" },
    ]);
    // The routine's own title/instructions are not this dialog's to rewrite.
    expect(apiPost.mock.calls[0][1]).not.toHaveProperty("title");
    expect(apiPost.mock.calls[0][1]).not.toHaveProperty("description");
    // And no raw trigger was posted through the Teams BFF.
    expect(post).not.toHaveBeenCalled();

    // The guided finish step still runs — same dialog both doors show.
    expect(await screen.findByText(STRIPE_RESULT.proxy_url)).toBeInTheDocument();
  });

  it("states what an appending shape will add before it is confirmed", async () => {
    connections = [GOOGLE];
    apiGet.mockResolvedValue([{ id: "ada@example.com", label: "ada@example.com" }]);
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /add trigger/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("radio", { name: /a connected app/i }));
    await user.click(within(dialog).getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "Google" }));
    await user.click(within(dialog).getByRole("button", { name: /select (an )?events?…/i }));
    await user.click(await screen.findByRole("option", { name: "Calendar changed" }));

    const disclosure = await within(dialog).findByText(/renewal step appended to the END/i);
    expect(disclosure).toHaveTextContent(/twice-weekly schedule trigger/i);
    expect(disclosure).toHaveTextContent(/Everything you wrote stays exactly as it is/i);
  });
});


// ── multi-event: one trigger, many events ──────────────────────────────────
//
// A Paperclip webhook trigger has no event filter, and Stripe lets ONE
// endpoint subscribe to many events — so the founder's three-event Revenue
// recovery routine is one URL to paste, not three.
describe("Multi-event — one trigger carries the whole selection", () => {
  it("submits every ticked event as one trigger, and drafts instructions that branch", async () => {
    connections = [STRIPE];
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: /new automation/i }));
    const dialog = await screen.findByRole("dialog");
    await pickStripeInvoiceEvent(dialog, user);
    await alsoPick(dialog, user, ["Subscription canceled", "Charge refunded"]);

    // The draft names all three and tells the agent to read the type first.
    expect(within(dialog).getByLabelText("Title")).toHaveValue(
      "Stripe: Invoice payment failed, Subscription canceled and Charge refunded",
    );
    const description = within(dialog).getByLabelText("Description") as HTMLTextAreaElement;
    expect(description.value).toContain("This trigger is armed for 3 events");
    expect(description.value).toContain("read the event type out of the payload FIRST");

    await user.click(within(dialog).getByRole("button", { name: /^Create$/ }));
    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    expect(apiPost.mock.calls[0][1]).toEqual({
      provider: "stripe",
      event_ids: [
        "invoice.payment_failed",
        "customer.subscription.deleted",
        "charge.refunded",
      ],
    });
  });

  it("summarizes the selection on the picker button", async () => {
    connections = [STRIPE];
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: /new automation/i }));
    const dialog = await screen.findByRole("dialog");
    await pickStripeInvoiceEvent(dialog, user);
    // One pick reads exactly as it did before multi-select.
    expect(
      within(dialog).getByRole("button", { name: "Invoice payment failed" }),
    ).toBeInTheDocument();

    await alsoPick(dialog, user, ["Subscription canceled", "Charge refunded"]);
    expect(
      within(dialog).getByRole("button", { name: /Invoice payment failed \+2 more/ }),
    ).toBeInTheDocument();
  });

  it("never clobbers a field the owner has typed into when events are toggled", async () => {
    connections = [STRIPE];
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: /new automation/i }));
    const dialog = await screen.findByRole("dialog");
    await pickStripeInvoiceEvent(dialog, user);

    const description = within(dialog).getByLabelText("Description");
    await user.clear(description);
    await user.type(description, "Email the customer, then tell me.");

    // Ticking another event re-derives the UNTOUCHED title but leaves the
    // dirtied description exactly as typed.
    await alsoPick(dialog, user, ["Charge refunded"]);
    expect(within(dialog).getByLabelText("Title")).toHaveValue(
      "Stripe: Invoice payment failed and Charge refunded",
    );
    expect(description).toHaveValue("Email the customer, then tell me.");

    await user.click(within(dialog).getByRole("button", { name: /^Create$/ }));
    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    expect(apiPost.mock.calls[0][1]).toEqual({
      provider: "stripe",
      event_ids: ["invoice.payment_failed", "charge.refunded"],
      description: "Email the customer, then tell me.",
    });
  });

  it("lists every event to subscribe next to the one URL in the finish dialog", async () => {
    connections = [STRIPE];
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /add trigger/i }));
    const dialog = await screen.findByRole("dialog");
    await pickStripeInvoiceEvent(dialog, user);
    await alsoPick(dialog, user, ["Charge refunded"]);
    await user.click(within(dialog).getByRole("button", { name: /^Add$/ }));

    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    expect(apiPost.mock.calls[0][1]).toEqual({
      provider: "stripe",
      event_ids: ["invoice.payment_failed", "charge.refunded"],
      routine_id: "r1",
    });
    // ONE URL, and the events to tick against it.
    expect(await screen.findByText(STRIPE_RESULT.proxy_url)).toBeInTheDocument();
    expect(await screen.findByText(/Subscribe this one endpoint to all 2 events/i)).toBeInTheDocument();
    expect(screen.getByText("invoice.payment_failed")).toBeInTheDocument();
    expect(screen.getByText("charge.refunded")).toBeInTheDocument();
  });
});


// ── the multi-select gate ─────────────────────────────────────────────────
describe("Providers whose deliveries name the event in a header stay single-event", () => {
  it("replaces the selection instead of accumulating, and submits one event", async () => {
    connections = [SENTRY];
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: /new automation/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("radio", { name: /a connected app/i }));
    await user.click(within(dialog).getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "Sentry" }));

    await user.click(within(dialog).getByRole("button", { name: /select an event/i }));
    await user.click(await screen.findByRole("option", { name: "Issue created or changed" }));
    // Picking a second REPLACES the first — an agent woken by one Sentry
    // trigger could not tell which of them fired (Isol8AI/isol8#1916).
    await user.click(within(dialog).getByRole("button", { name: /issue created or changed/i }));
    await user.click(await screen.findByRole("option", { name: "Comment" }));

    expect(within(dialog).getByLabelText("Title")).toHaveValue("Sentry: Comment");
    await user.click(within(dialog).getByRole("button", { name: /^Create$/ }));
    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    expect(apiPost.mock.calls[0][1]).toEqual({ provider: "sentry", event_ids: ["comment"] });
  });

  it("shows a plain-string 400 detail instead of 'ApiError'", async () => {
    // Every caller-facing 400 from this route is HTTPException(detail="…"),
    // so `detail` is the string itself — reading only detail.message rendered
    // these as "ApiError: API 400".
    connections = [SENTRY];
    apiPost.mockRejectedValueOnce({
      body: {
        detail: "an agent woken by one Sentry trigger can't tell which event fired from the payload alone",
      },
    });
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: /new automation/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("radio", { name: /a connected app/i }));
    await user.click(within(dialog).getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "Sentry" }));
    await user.click(within(dialog).getByRole("button", { name: /select an event/i }));
    await user.click(await screen.findByRole("option", { name: "Issue created or changed" }));
    await user.click(within(dialog).getByRole("button", { name: /^Create$/ }));

    expect(
      await screen.findByText(/can't tell which event fired from the payload alone/i),
    ).toBeInTheDocument();
  });
});
