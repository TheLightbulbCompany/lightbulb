import { fireEvent, render, screen } from "@testing-library/react-native";
import type { SessionResumeFlow } from "@lightbulb/shared";

import { ResumeDelivery, SessionResume } from "../SessionResume";

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

const flow = (changes: Partial<SessionResumeFlow> = {}): SessionResumeFlow =>
  ({ available: true, status: null, saving: false, error: null, resume: jest.fn(async () => true), chip: null, ...changes });

it("sends the message with the resume and says it runs in plan mode on the owner's Mac", async () => {
  const f = flow();
  await render(<SessionResume flow={f} own={false} ownerName="Alice" />);
  expect(screen.getByText("Starts it again on Alice’s Mac in plan mode. Alice sees it there and can end it.")).toBeTruthy();
  await fireEvent.changeText(screen.getByLabelText("Message to send when it resumes"), "Carry on");
  await fireEvent.press(screen.getByLabelText("Resume and send"));
  expect(f.resume).toHaveBeenCalledWith("Carry on");
});

it("shows a waiting resume and no button", async () => {
  await render(<SessionResume flow={flow({ available: false, status: "You asked to resume this session." })} own={false} ownerName="Alice" />);
  expect(screen.getByText("You asked to resume this session.")).toBeTruthy();
  expect(screen.queryByLabelText("Resume")).toBeNull();
});

it("says Delivery unknown for a first message whose outcome was lost", async () => {
  await render(<ResumeDelivery flow={flow({ chip: { label: "Delivery unknown", tone: "warn", detail: "Do not send it again." } })} />);
  expect(screen.getByText("First message: Delivery unknown · Do not send it again.")).toBeTruthy();
});
