// @vitest-environment jsdom
import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { DesktopPermissions, LocalSessions, ShareEverySessionLive, providerSetupLine } from "../LocalSessions";
import { DesktopPermissionsGate } from "../DesktopPermissionsGate";
import { DEFAULT_DESKTOP_PERMISSIONS, writeDesktopPermissionGrant } from "@/lib/desktopPermissions";
vi.mock("@/lib/analytics", () => ({capture:vi.fn()}));
// The agent-session switches read /users/me/prefs; a stub keeps that off `auth.getToken`, which these tests script.
const prefs = {agentSendWithoutAsking:false,sessionsReachableFromBackgroundWork:false};
vi.mock("@/lib/api", () => ({useApi: () => ({get: async () => prefs, put: async () => prefs})}));

const auth = vi.hoisted(()=>({userId:"alice",orgId:"org",getToken:vi.fn()}));
const org = vi.hoisted(()=>({organization:{id:"org",name:"Acme"} as {id:string;name:string}|null}));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => auth, useOrganization: () => org }));
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
  // Reads only: the two status commands change nothing.
  expect(invoke.mock.calls.every(([command]) => command === "local_sessions_status" || command === "shell_integration_status")).toBe(true);
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
beforeEach(() => { invoke.mockReset();auth.getToken.mockReset().mockResolvedValue("jwt"); auth.userId="alice";auth.orgId="org";org.organization={id:"org",name:"Acme"};Object.assign(window,{__TAURI__:{core:{invoke}}}); });
it("explains the full audience and requires an already connected Mac", async () => {
  invoke.mockResolvedValue({enrollment:null,pairedWorkspaceId:null,status:""});
  render(<LocalSessions />);
  await waitFor(()=>expect(screen.getByText("Connect this Mac first.")).toBeTruthy());
  expect(screen.getByText(/Something typed in one conversation can be visible in another/)).toBeTruthy();
  expect(screen.getByText(/can be visible in another, and someone added/)).toBeTruthy();
  expect(screen.getByText("claude plugin marketplace add TheLightbulbCompany/lightbulb-plugin")).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/org_/);
  expect(screen.getByRole("button",{name:"Turn on sharing"}).hasAttribute("disabled")).toBe(true);
});
it("shows checking status until the native status resolves", async()=>{
  let resolveStatus!: (state: { enrollment: null; pairedWorkspaceId: string; status: string }) => void;
  invoke.mockImplementation(async cmd=>cmd==="local_sessions_status" ? new Promise(resolve=>{resolveStatus=resolve;}) : false);
  render(<LocalSessions />);
  expect(await screen.findByRole("status")).toHaveTextContent("Checking this Mac…");
  expect(screen.getByRole("button",{name:"Turn on sharing"})).toBeDisabled();
  await act(async()=>resolveStatus({enrollment:null,pairedWorkspaceId:"org",status:""}));
  await waitFor(()=>expect(screen.queryByRole("status")).toBeNull());
});
it("shows the status error instead of indefinite loading when native status rejects", async()=>{
  invoke.mockImplementation(async cmd=>{ if(cmd==="local_sessions_status") throw new Error("Bridge unavailable"); return false; });
  render(<LocalSessions />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Bridge unavailable");
  expect(screen.queryByRole("status")).toBeNull();
});
it("stops locally even when token refresh never resolves", async()=>{
  auth.getToken.mockReturnValue(new Promise(()=>{}));
  invoke.mockResolvedValue({enrollment:{ownerId:"org",humanUserId:"alice",enabled:true,paused:false},pairedWorkspaceId:"org",status:"connected"});
  render(<LocalSessions />);
  await userEvent.click(await screen.findByRole("button",{name:"Turn off sharing"}));
  await waitFor(()=>expect(invoke).toHaveBeenCalledWith("local_sessions_set",{enabled:false,token:null,humanUserId:"alice",workspaceId:"org"}));
});
it("only explicit consent enrolls the current workspace and uses the human token", async () => {
  invoke.mockResolvedValue({enrollment:null,pairedWorkspaceId:"org",status:""});
  render(<LocalSessions />);
  await waitFor(()=>expect(screen.getByRole("button",{name:"Turn on sharing"})).toBeTruthy());
  expect(invoke.mock.calls.every(([cmd])=>cmd === "local_sessions_status" || cmd === "shell_integration_status")).toBe(true);
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.click(screen.getByRole("button",{name:"Turn on sharing"}));
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
  await userEvent.click(await screen.findByRole("button",{name:"Turn off sharing"}));
  await screen.findByRole("button",{name:"Turn on sharing"});
  if(change==="enable") {
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByRole("button",{name:"Turn on sharing"}));
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
  await userEvent.click(await screen.findByRole("button",{name:"Turn off sharing"}));
  await waitFor(()=>expect(invoke).toHaveBeenCalledWith("local_sessions_revoke",{revision:19,token:"jwt",humanUserId:"alice",workspaceId:"org"}));
});

it("keeps failed setup recoverable and reports remote cleanup failure without offering enable", async()=>{
  invoke.mockImplementation(async(command)=>{
    if(command==="local_sessions_set") return 21;
    if(command==="local_sessions_revoke") throw new Error("Remote cleanup unavailable");
    return {enrollment:{ownerId:"org",humanUserId:"alice",enabled:false,paused:true,recoveryPending:true,remoteDisabled:false},pairedWorkspaceId:"org",status:"paused"};
  });
  render(<LocalSessions />);
  const retry = await screen.findByRole("button",{name:"Finish turning off"});
  expect(screen.getByText(/Earlier history may still be shared/)).toBeTruthy();
  expect(screen.queryByRole("button",{name:"Turn on sharing"})).toBeNull();
  await userEvent.click(retry);
  await waitFor(()=>expect(invoke).toHaveBeenCalledWith("local_sessions_revoke",{revision:21,token:"jwt",humanUserId:"alice",workspaceId:"org"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Remote cleanup unavailable");
  expect(screen.getByRole("button",{name:"Finish turning off"})).not.toBeDisabled();
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
  expect(await screen.findByRole("button",{name:"Finish turning off"})).toBeDisabled();
  auth.userId="alice";
  view.rerender(<LocalSessions />);
  await userEvent.click(await screen.findByRole("button",{name:"Finish turning off"}));
  expect(await screen.findByRole("button",{name:"Turn on sharing"})).toBeDisabled();
  expect(screen.queryByText(/Earlier history may still be shared/)).toBeNull();
});

it("re-reports desktop auth before enabling to heal the sign-in race", async () => {
  invoke.mockResolvedValue({enrollment:null,pairedWorkspaceId:"org",status:""});
  render(<LocalSessions />);
  await userEvent.click(await screen.findByRole("checkbox"));
  await userEvent.click(screen.getByRole("button",{name:"Turn on sharing"}));
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
  await userEvent.click(screen.getByRole("button",{name:"Turn on sharing"}));
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
  await userEvent.click(screen.getByRole("button",{name:"Turn on sharing"}));
  expect(await screen.findByRole("button",{name:"Finish turning off"})).not.toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent("Claude hook setup failed");
  expect(screen.queryByRole("button",{name:"Turn on sharing"})).toBeNull();
});
it("Settings switches read the Mac's stored consent and write each change through", async () => {
  invoke.mockImplementation(async (command) => command === "desktop_permissions_get"
    ? {consented:true,shell:true,browser:true,files:false,screen:false,paperclip:true,updatedAt:"1"}
    : undefined);
  render(<DesktopPermissions />);
  const paperclip = await screen.findByRole("switch", {name:"Paperclip actions"});
  expect(paperclip).toBeChecked();
  expect(screen.getByRole("switch", {name:"Files"})).not.toBeChecked();
  await userEvent.click(paperclip);
  expect(invoke).toHaveBeenCalledWith("desktop_permissions_set", {permissions:{shell:true,browser:true,files:false,screen:false,paperclip:false}});
});
it("shows per-provider setup read from disk and retries setup without re-enrolling", async () => {
  invoke.mockImplementation(async command => command === "local_sessions_repair" ? [] : {
    enrollment:{ownerId:"org",humanUserId:"alice",enabled:true,paused:false}, pairedWorkspaceId:"org", status:"connected",
    providers:{claude:{cli:true,mcp:"collision",hooks:"disabled"},codex:{cli:false,mcp:"missing",hooks:"missing"}},
  });
  render(<LocalSessions />);
  expect(await screen.findByText("Claude Code — Needs attention: another lightbulb MCP server is configured; Claude hooks are disabled in settings.json")).toBeInTheDocument();
  expect(screen.getByText("Codex — Not installed")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", {name:"Retry setup"}));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("local_sessions_repair"));
  expect(invoke.mock.calls.some(([command]) => command === "local_sessions_set")).toBe(false);
  expect(providerSetupLine("Codex", {cli:true,mcp:"configured",hooks:"configured"})).toBe("Codex — Ready for new sessions");
  expect(screen.getByText("claude plugin marketplace add TheLightbulbCompany/lightbulb-plugin")).toBeInTheDocument();
});
it("turns Share every session live on and off through the desktop, off by default", async () => {
  invoke.mockImplementation(async (command, args) => command === "shell_integration_set" ? args.enabled : false);
  render(<ShareEverySessionLive />);
  const box = await screen.findByRole("switch", {name:"Share every session live"});
  expect(box).not.toBeChecked();
  await userEvent.click(box);
  await waitFor(() => expect(box).toBeChecked());
  expect(invoke).toHaveBeenCalledWith("shell_integration_set", {enabled:true});
});
it("turns Share every session live on at pairing unless the person turns it off", async () => {
  localStorage.removeItem("lightbulb.desktop.liveSharingOffer");
  invoke.mockImplementation(async (command) => command === "local_sessions_set" ? 1 : { enrollment:null, pairedWorkspaceId:"org", status:"paused" });
  render(<DesktopPermissionsGate />);
  const live = await screen.findByRole("button", {name:/Share every session live/});
  expect(live).toHaveAttribute("aria-pressed", "true");
  expect(live).toHaveTextContent("~/.zshrc, ~/.bashrc and your bash login file");
  await userEvent.click(screen.getByRole("button", {name:"Continue"}));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("shell_integration_set", {enabled:true}));
});
it("records that pairing made the Share every session live offer, also when it was turned off", async () => {
  localStorage.removeItem("lightbulb.desktop.liveSharingOffer");
  invoke.mockImplementation(async (command) => command === "local_sessions_set" ? 1 : { enrollment:null, pairedWorkspaceId:"org", status:"paused" });
  render(<DesktopPermissionsGate />);
  await userEvent.click(await screen.findByRole("button", {name:/Share every session live/}));
  await userEvent.click(screen.getByRole("button", {name:"Continue"}));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  // An explicit off is always applied: the block may be there although this Mac's storage says it was never offered.
  expect(invoke).toHaveBeenCalledWith("shell_integration_set", {enabled:false});
  expect(invoke).not.toHaveBeenCalledWith("shell_integration_set", {enabled:true});
  // The once-per-Mac notice for Macs paired before the switch existed must not ask again.
  expect(localStorage.getItem("lightbulb.desktop.liveSharingOffer")).toBe("1");
});
it("keeps a Mac's own answer when it pairs again: off stays off, and turning it off there removes the block", async () => {
  // This Mac was offered the switch before (an earlier pairing, or the notice) and its rc files say no.
  localStorage.setItem("lightbulb.desktop.liveSharingOffer", "1");
  let on = false;
  invoke.mockImplementation(async (command) => command === "shell_integration_status" ? on : command === "local_sessions_set" ? 1 : { enrollment:null, pairedWorkspaceId:"org", status:"paused" });
  const first = render(<DesktopPermissionsGate />);
  await waitFor(() => expect(screen.getByRole("button", {name:/Share every session live/})).toHaveAttribute("aria-pressed", "false"));
  await userEvent.click(screen.getByRole("button", {name:"Continue"}));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  expect(invoke.mock.calls.some(([command]) => command === "shell_integration_set")).toBe(false);
  first.unmount();
  localStorage.removeItem("isol8.desktop.permissions.v1"); // the grant that pairing just wrote: this Mac pairs once more
  // It said yes before: still pressed, and unpressing it here is the Settings switch turned off.
  on = true;
  render(<DesktopPermissionsGate />);
  const live = await screen.findByRole("button", {name:/Share every session live/});
  expect(live).toHaveAttribute("aria-pressed", "true");
  await userEvent.click(live);
  await userEvent.click(screen.getByRole("button", {name:"Continue"}));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("shell_integration_set", {enabled:false}));
  localStorage.removeItem("lightbulb.desktop.liveSharingOffer");
});
it("applies an explicit off when the block is there but local storage was cleared", async () => {
  localStorage.removeItem("lightbulb.desktop.liveSharingOffer");
  invoke.mockImplementation(async (command) => command === "shell_integration_status" ? true : command === "local_sessions_set" ? 1 : { enrollment:null, pairedWorkspaceId:"org", status:"paused" });
  render(<DesktopPermissionsGate />);
  const live = await screen.findByRole("button", {name:/Share every session live/});
  expect(live).toHaveAttribute("aria-pressed", "true");
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("shell_integration_status"));
  await userEvent.click(live);
  await userEvent.click(screen.getByRole("button", {name:"Continue"}));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("shell_integration_set", {enabled:false}));
  localStorage.removeItem("lightbulb.desktop.liveSharingOffer");
});
it("never writes the switch for an already-offered Mac before its stored answer is known, or when it cannot be read", async () => {
  localStorage.setItem("lightbulb.desktop.liveSharingOffer", "1");
  // The read is still out: the switch waits, and Continue leaves the rc files alone.
  let answer: (on: boolean) => void = () => {};
  invoke.mockImplementation(async (command) => command === "shell_integration_status" ? new Promise<boolean>(resolve => { answer = resolve; }) : command === "local_sessions_set" ? 1 : { enrollment:null, pairedWorkspaceId:"org", status:"paused" });
  const first = render(<DesktopPermissionsGate />);
  const waiting = await screen.findByRole("button", {name:/Share every session live/});
  expect(waiting).toBeDisabled();
  expect(waiting).toHaveAttribute("aria-pressed", "false");
  await userEvent.click(screen.getByRole("button", {name:"Continue"}));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  expect(invoke.mock.calls.some(([command]) => command === "shell_integration_set")).toBe(false);
  answer(false);
  first.unmount();
  localStorage.removeItem("isol8.desktop.permissions.v1");
  // The read failed: the same.
  invoke.mockImplementation(async (command) => { if (command === "shell_integration_status") throw new Error("no"); return command === "local_sessions_set" ? 1 : { enrollment:null, pairedWorkspaceId:"org", status:"paused" }; });
  render(<DesktopPermissionsGate />);
  expect(await screen.findByRole("button", {name:/Share every session live/})).toBeDisabled();
  await userEvent.click(screen.getByRole("button", {name:"Continue"}));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  expect(invoke.mock.calls.some(([command]) => command === "shell_integration_set")).toBe(false);
  localStorage.removeItem("lightbulb.desktop.liveSharingOffer");
});

it("names the workspace sharing is on in, never its raw id, and shows the desktop status as its own note", async () => {
  const on = (status: string) => ({enrollment:{ownerId:"org",humanUserId:"alice",enabled:true,paused:false},pairedWorkspaceId:"org",status});
  invoke.mockResolvedValue(on("Setup retried; Claude Code and Codex are configured for new sessions."));
  const view = render(<LocalSessions />);
  expect(await screen.findByText("On · Acme")).toBeInTheDocument();
  expect(screen.getByText("Setup retried; Claude Code and Codex are configured for new sessions.")).toBeInTheDocument();
  // The healthy state is the single word "connected": "On" already says it.
  invoke.mockResolvedValue(on("connected"));
  await userEvent.click(screen.getByRole("button", {name:"Check again"}));
  await waitFor(() => expect(screen.queryByText(/Setup retried/)).toBeNull());
  expect(screen.queryByText("connected")).toBeNull();
  expect(screen.queryByRole("checkbox")).toBeNull();
  org.organization = {id:"org_3H3vi",name:"Other"};
  view.rerender(<LocalSessions />);
  expect(screen.getByText("On · another workspace")).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/org_/);
});
it("says paused and other-workspace states in plain words", async () => {
  invoke.mockResolvedValue({enrollment:{ownerId:"org",humanUserId:"alice",enabled:true,paused:true},pairedWorkspaceId:"org",status:"paused"});
  const view = render(<LocalSessions />);
  expect(await screen.findByText("Paused until you sign in.")).toBeInTheDocument();
  view.unmount();
  invoke.mockResolvedValue({enrollment:null,pairedWorkspaceId:"org_elsewhere",status:""});
  render(<LocalSessions />);
  expect(await screen.findByText(/This Mac is connected to another workspace/)).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/org_/);
});
