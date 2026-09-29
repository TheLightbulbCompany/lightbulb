// @vitest-environment jsdom
import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { DesktopPermissions, LocalSessions } from "../LocalSessions";
import { DesktopPermissionsGate } from "../DesktopPermissionsGate";
import { DEFAULT_DESKTOP_PERMISSIONS, writeDesktopPermissionGrant } from "@/lib/desktopPermissions";
vi.mock("@/lib/analytics", () => ({capture:vi.fn()}));

const auth = vi.hoisted(()=>({userId:"alice",orgId:"org",getToken:vi.fn()}));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => auth }));
const invoke = vi.fn();
it("lets Basic only proceed even while initial sharing status is pending", async () => {
  invoke.mockImplementationOnce(() => new Promise(() => {})).mockResolvedValue({enrollment:null,pairedWorkspaceId:"org",status:"paused"});
  render(<DesktopPermissionsGate />);
  expect(screen.getByRole("button", {name:"Continue"})).toBeDisabled();
  await userEvent.click(screen.getByRole("button", {name:"Basic only"}));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("local_sessions_set", {enabled:false,token:null,humanUserId:"alice",workspaceId:"org"}));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
});
it("offers sharing with the other permissions once and sends nothing until Continue", async () => {
  invoke.mockImplementation(async (command) => {
    if (command === "local_sessions_set") return 1;
    return { enrollment:null, pairedWorkspaceId:"org", status:"paused" };
  });
  render(<DesktopPermissionsGate />);
  expect(await screen.findByRole("button", {name:/Share Claude & Codex sessions/})).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("alertdialog", {name:"Connect this Mac"})).toBeInTheDocument();
  expect(screen.getByRole("button", {name:/Browser/})).toBeInTheDocument();
  expect(invoke.mock.calls.every(([command]) => command === "local_sessions_status")).toBe(true);
  await userEvent.click(screen.getByRole("button", {name:"Continue"}));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("local_sessions_set", {enabled:true,token:"jwt",humanUserId:"alice",workspaceId:"org"}));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
});
it("keeps an initial off choice off after reopening without requesting a token", async () => {
  invoke.mockResolvedValue({ enrollment:null, pairedWorkspaceId:"org", status:"paused" });
  const view = render(<DesktopPermissionsGate />);
  await userEvent.click(await screen.findByRole("button", {name:/Share Claude & Codex sessions/}));
  await userEvent.click(screen.getByRole("button", {name:"Continue"}));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  expect(invoke).toHaveBeenCalledWith("local_sessions_set", {enabled:false,token:null,humanUserId:"alice",workspaceId:"org"});
  expect(auth.getToken).not.toHaveBeenCalled();
  expect(invoke.mock.calls.some(([command]) => command === "local_sessions_revoke")).toBe(false);
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  view.unmount();
  await act(async () => { render(<DesktopPermissionsGate />); });
  expect(screen.queryByRole("alertdialog")).toBeNull();
});
it("preserves an enrolled session when the Mac permission screen is shown again", async () => {
  invoke.mockImplementation(async () =>
    ({ enrollment:{ownerId:"org",humanUserId:"alice",enabled:true,paused:false}, pairedWorkspaceId:"org", status:"connected" }));
  render(<DesktopPermissionsGate />);
  await waitFor(() => expect(screen.getByRole("button", {name:"Continue"})).toBeEnabled());
  await userEvent.click(screen.getByRole("button", {name:"Continue"}));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  expect(invoke).toHaveBeenCalledWith("desktop_permissions_set", {permissions:DEFAULT_DESKTOP_PERMISSIONS});
  // Consenting to Browser needs no setup call (the agent Chrome starts on first use); sessions are untouched.
  expect(invoke.mock.calls.some(([command]) => String(command).startsWith("local_browser_"))).toBe(false);
  expect(invoke.mock.calls.some(([command]) => String(command).startsWith("local_sessions_") && command !== "local_sessions_status")).toBe(false);
});
it("keeps failed remote-history disable visible instead of silently completing setup", async () => {
  invoke.mockImplementation(async command => command === "local_sessions_set" ? 1 : {
    enrollment:{ownerId:"other",humanUserId:"bob",enabled:false,paused:true,recoveryPending:true},pairedWorkspaceId:null,status:"paused",
  });
  render(<DesktopPermissionsGate />);
  await waitFor(() => expect(screen.getByRole("button", {name:"Continue"})).toBeEnabled());
  await userEvent.click(screen.getByRole("button", {name:"Basic only"}));
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  expect(screen.getByRole("button", {name:/Browser/})).toHaveAttribute("aria-pressed", "false");
  expect(invoke).toHaveBeenCalledWith("local_sessions_set", {enabled:false,token:null,humanUserId:"alice",workspaceId:"org"});
});
it("stops sharing on retry even when failed enrollment status has not arrived", async () => {
  invoke.mockImplementation(async (command, args) => {
    if (command === "local_sessions_set" && args.enabled) throw new Error("Enrollment unavailable");
    return {enrollment:null,pairedWorkspaceId:"org",status:"paused"};
  });
  render(<DesktopPermissionsGate />);
  await waitFor(() => expect(screen.getByRole("button", {name:"Continue"})).toBeEnabled());
  await userEvent.click(screen.getByRole("button", {name:"Continue"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Enrollment unavailable");
  expect(screen.getByRole("button", {name:/Browser/})).toBeDisabled();
  expect(screen.queryByRole("button", {name:"Basic only"})).toBeNull();
  await userEvent.click(screen.getByRole("button", {name:"Continue without sharing"}));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  expect(invoke).toHaveBeenCalledWith("local_sessions_set", {enabled:false,token:null,humanUserId:"alice",workspaceId:"org"});
});
it.each([true, false])("does not re-prompt or change saved enrollment on relaunch (enabled=%s)", async (enabled) => {
  invoke.mockResolvedValue({ enrollment:{ownerId:"org",humanUserId:"alice",enabled,paused:false}, pairedWorkspaceId:"org", status:"connected" });
  writeDesktopPermissionGrant(DEFAULT_DESKTOP_PERMISSIONS);
  render(<DesktopPermissionsGate />);
  await waitFor(() => expect(invoke).toHaveBeenCalled());
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(screen.queryByRole("button", {name:"Continue"})).toBeNull();
  expect(invoke.mock.calls.some(([command]) => String(command).startsWith("local_sessions_") && command !== "local_sessions_status")).toBe(false);
});
beforeEach(() => { invoke.mockReset();auth.getToken.mockReset().mockResolvedValue("jwt"); auth.userId="alice";auth.orgId="org";Object.assign(window,{__TAURI__:{core:{invoke}}}); });
it("explains the full audience and requires an already connected Mac", async () => {
  invoke.mockResolvedValue({enrollment:null,pairedWorkspaceId:null,status:""});
  render(<LocalSessions />);
  await waitFor(()=>expect(screen.getByText(/Connect this Mac first/)).toBeTruthy());
  expect(screen.getByText(/Contributions can be visible across conversations/)).toBeTruthy();
  expect(screen.getByText(/Coverage:/)).toHaveTextContent("Claude Code 2.1.145 or newer on the 2.1 line (follow-ups from 2.1.224; verified on 2.1.268 and 2.1.269) and Codex 0.149.0");
  expect(screen.getByRole("button",{name:"Enable local-session sharing"}).hasAttribute("disabled")).toBe(true);
});
it("shows checking status until the native status resolves", async()=>{
  let resolveStatus!: (state: { enrollment: null; pairedWorkspaceId: string; status: string }) => void;
  invoke.mockImplementationOnce(()=>new Promise(resolve=>{resolveStatus=resolve;}));
  render(<LocalSessions />);
  expect(await screen.findByRole("status")).toHaveTextContent("Checking desktop status…");
  expect(screen.getByRole("button",{name:"Enable local-session sharing"})).toBeDisabled();
  await act(async()=>resolveStatus({enrollment:null,pairedWorkspaceId:"org",status:""}));
  await waitFor(()=>expect(screen.queryByRole("status")).toBeNull());
});
it("shows the status error instead of indefinite loading when native status rejects", async()=>{
  invoke.mockRejectedValueOnce(new Error("Bridge unavailable"));
  render(<LocalSessions />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Bridge unavailable");
  expect(screen.queryByRole("status")).toBeNull();
});
it("stops locally even when token refresh never resolves", async()=>{
  auth.getToken.mockReturnValue(new Promise(()=>{}));
  invoke.mockResolvedValue({enrollment:{ownerId:"org",humanUserId:"alice",enabled:true,paused:false},pairedWorkspaceId:"org",status:"connected"});
  render(<LocalSessions />);
  await userEvent.click(await screen.findByRole("button",{name:"Disable local-session sharing"}));
  await waitFor(()=>expect(invoke).toHaveBeenCalledWith("local_sessions_set",{enabled:false,token:null,humanUserId:"alice",workspaceId:"org"}));
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

it.each(["enable", "user", "workspace"])("drops delayed-token cleanup after a new %s", async(change)=>{
  let resolveToken!: (token:string)=>void;
  auth.getToken.mockImplementationOnce(()=>new Promise<string>(resolve=>{resolveToken=resolve;})).mockResolvedValue("new-jwt");
  let enabled=true;
  invoke.mockImplementation(async(command,args)=>{
    if(command==="local_sessions_set") {enabled=args.enabled;return 7;}
    return {enrollment:{ownerId:"org",humanUserId:"alice",enabled,paused:false},pairedWorkspaceId:"org",status:"connected"};
  });
  const view=render(<LocalSessions />);
  await userEvent.click(await screen.findByRole("button",{name:"Disable local-session sharing"}));
  await screen.findByRole("button",{name:"Enable local-session sharing"});
  if(change==="enable") {
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByRole("button",{name:"Enable local-session sharing"}));
    await waitFor(()=>expect(invoke).toHaveBeenCalledWith("local_sessions_set",{enabled:true,token:"new-jwt",humanUserId:"alice",workspaceId:"org"}));
  } else {
    if(change==="user") auth.userId="bob";else auth.orgId="other";
    view.rerender(<LocalSessions />);
  }
  await act(async()=>resolveToken("old-jwt"));
  expect(invoke.mock.calls.some(([cmd])=>cmd==="local_sessions_revoke")).toBe(false);
});

it("passes the native disable revision to optional remote cleanup",async()=>{
  invoke.mockImplementation(async(command)=>command==="local_sessions_set" ? 19 : {enrollment:{ownerId:"org",humanUserId:"alice",enabled:true,paused:false},pairedWorkspaceId:"org",status:"connected"});
  render(<LocalSessions />);
  await userEvent.click(await screen.findByRole("button",{name:"Disable local-session sharing"}));
  await waitFor(()=>expect(invoke).toHaveBeenCalledWith("local_sessions_revoke",{revision:19,token:"jwt",humanUserId:"alice",workspaceId:"org"}));
});

it("keeps failed setup recoverable and reports remote cleanup failure without offering enable", async()=>{
  invoke.mockImplementation(async(command)=>{
    if(command==="local_sessions_set") return 21;
    if(command==="local_sessions_revoke") throw new Error("Remote cleanup unavailable");
    return {enrollment:{ownerId:"org",humanUserId:"alice",enabled:false,paused:true,recoveryPending:true,remoteDisabled:false},pairedWorkspaceId:"org",status:"paused"};
  });
  render(<LocalSessions />);
  const retry = await screen.findByRole("button",{name:"Retry remote-history disable"});
  expect(screen.getByText(/retained server history may still be shared/i)).toBeTruthy();
  expect(screen.queryByRole("button",{name:"Enable local-session sharing"})).toBeNull();
  await userEvent.click(retry);
  await waitFor(()=>expect(invoke).toHaveBeenCalledWith("local_sessions_revoke",{revision:21,token:"jwt",humanUserId:"alice",workspaceId:"org"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Remote cleanup unavailable");
  expect(screen.getByRole("button",{name:"Retry remote-history disable"})).not.toBeDisabled();
});

it("only offers a new enable after confirmed remote disable and never cleans up another identity", async()=>{
  let pending=true;
  invoke.mockImplementation(async(command)=>{
    if(command==="local_sessions_set") return 22;
    if(command==="local_sessions_revoke") {pending=false;return;}
    return {enrollment:{ownerId:"org",humanUserId:"alice",enabled:false,paused:true,recoveryPending:pending,remoteDisabled:!pending},pairedWorkspaceId:"org",status:"paused"};
  });
  auth.userId="bob";
  const view=render(<LocalSessions />);
  expect(await screen.findByRole("button",{name:"Retry remote-history disable"})).toBeDisabled();
  auth.userId="alice";
  view.rerender(<LocalSessions />);
  await userEvent.click(await screen.findByRole("button",{name:"Retry remote-history disable"}));
  expect(await screen.findByRole("button",{name:"Enable local-session sharing"})).toBeDisabled();
  expect(screen.queryByText(/retained server history may still be shared/i)).toBeNull();
});

it("re-reports desktop auth before enabling to heal the sign-in race", async () => {
  invoke.mockResolvedValue({enrollment:null,pairedWorkspaceId:"org",status:""});
  render(<LocalSessions />);
  await userEvent.click(await screen.findByRole("checkbox"));
  await userEvent.click(screen.getByRole("button",{name:"Enable local-session sharing"}));
  await waitFor(()=>expect(invoke).toHaveBeenCalledWith("local_sessions_auth",{humanUserId:"alice"}));
  await waitFor(()=>expect(invoke).toHaveBeenCalledWith("local_sessions_set",{enabled:true,token:"jwt",humanUserId:"alice",workspaceId:"org"}));
  const calls = invoke.mock.calls.map(([command])=>command);
  expect(calls.indexOf("local_sessions_auth")).toBeLessThan(calls.indexOf("local_sessions_set"));
});
it("reports a token failure instead of a misleading sign-in error", async () => {
  auth.getToken.mockRejectedValueOnce(new Error("network down"));
  invoke.mockResolvedValue({enrollment:null,pairedWorkspaceId:"org",status:""});
  render(<LocalSessions />);
  await userEvent.click(await screen.findByRole("checkbox"));
  await userEvent.click(screen.getByRole("button",{name:"Enable local-session sharing"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not get a sign-in token");
  expect(invoke.mock.calls.some(([command])=>command==="local_sessions_set")).toBe(false);
});
it("refreshes a failed enable into stopped recovery instead of losing its Disable action", async()=>{
  let pending=false;
  invoke.mockImplementation(async(command)=>{
    if(command==="local_sessions_set") {pending=true;throw new Error("Claude hook setup failed");}
    return {enrollment:pending ? {ownerId:"org",humanUserId:"alice",enabled:false,paused:true,recoveryPending:true,remoteDisabled:false} : null,pairedWorkspaceId:"org",status:"paused"};
  });
  render(<LocalSessions />);
  await userEvent.click(await screen.findByRole("checkbox"));
  await userEvent.click(screen.getByRole("button",{name:"Enable local-session sharing"}));
  expect(await screen.findByRole("button",{name:"Retry remote-history disable"})).not.toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent("Claude hook setup failed");
  expect(screen.queryByRole("button",{name:"Enable local-session sharing"})).toBeNull();
});
it("Settings switches read the Mac's stored consent and write each change through", async () => {
  invoke.mockImplementation(async (command) => command === "desktop_permissions_get"
    ? {consented:true,shell:true,browser:true,files:false,screen:false,paperclip:true,updatedAt:"1"}
    : undefined);
  render(<DesktopPermissions />);
  const paperclip = await screen.findByRole("checkbox", {name:/Paperclip actions/});
  expect(paperclip).toBeChecked();
  expect(screen.getByRole("checkbox", {name:/Files/})).not.toBeChecked();
  await userEvent.click(paperclip);
  expect(invoke).toHaveBeenCalledWith("desktop_permissions_set", {permissions:{shell:true,browser:true,files:false,screen:false,paperclip:false}});
});
