// @vitest-environment jsdom
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { LocalSessions } from "../LocalSessions";

vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId:"alice",orgId:"org",getToken:async()=>"jwt" }) }));
const invoke = vi.fn();
beforeEach(() => { invoke.mockReset();Object.assign(window,{__TAURI__:{core:{invoke}}}); });
it("explains the full audience and requires an already connected Mac", async () => {
  invoke.mockResolvedValue({enrollment:null,pairedWorkspaceId:null,status:""});
  render(<LocalSessions />);
  await waitFor(()=>expect(screen.getByText(/Connect this Mac first/)).toBeTruthy());
  expect(screen.getByText(/Contributions can be visible across conversations/)).toBeTruthy();
  expect(screen.getByRole("button",{name:"Enable local-session sharing"}).hasAttribute("disabled")).toBe(true);
});
it("only explicit consent enrolls the current workspace and uses the human token", async () => {
  invoke.mockResolvedValue({enrollment:null,pairedWorkspaceId:"org",status:""});
  render(<LocalSessions />);
  await waitFor(()=>expect(screen.getByRole("button",{name:"Enable local-session sharing"})).toBeTruthy());
  expect(invoke.mock.calls.every(([cmd])=>cmd === "local_sessions_status")).toBe(true);
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.click(screen.getByRole("button",{name:"Enable local-session sharing"}));
  await waitFor(()=>expect(invoke).toHaveBeenCalledWith("local_sessions_set",{enabled:true,token:"jwt",humanUserId:"alice",workspaceId:"org"}));
});
