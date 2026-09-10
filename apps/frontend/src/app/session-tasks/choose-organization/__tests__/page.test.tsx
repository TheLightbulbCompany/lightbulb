import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

const taskProps = vi.hoisted(() => vi.fn());

vi.mock("@clerk/nextjs", () => ({
  TaskChooseOrganization: (props: { redirectUrlComplete: string }) => {
    taskProps(props);
    return <div>Native choose workspace task</div>;
  },
}));

import ChooseOrganizationTaskPage from "../page";

it("renders Clerk's native organization task and completes at chat", () => {
  render(<ChooseOrganizationTaskPage />);

  expect(screen.getByText("Native choose workspace task")).toBeInTheDocument();
  expect(taskProps).toHaveBeenCalledWith({ redirectUrlComplete: "/chat" });
});
