import { recapLine } from "../TrialRecapCard";

jest.mock("@clerk/expo", () => ({ useAuth: () => ({ getToken: jest.fn() }), useOrganization: () => ({}) }));
jest.mock("../../../lib/localStore", () => ({
  LocalStore: class {
    read() {
      return {};
    }
    write() {}
  },
}));

describe("trial recap line", () => {
  it("shows only counts above zero", () => {
    expect(recapLine({ show: true, handoffs: 2, approvals: 1, agents: 3, people: 4 })).toBe(
      "2 handoffs · 1 approval · 3 agents did work for 4 people",
    );
    expect(recapLine({ show: true, handoffs: 0, approvals: 0, agents: 0, people: 2 })).toBeNull();
  });
});
