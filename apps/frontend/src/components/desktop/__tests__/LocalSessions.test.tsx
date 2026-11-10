// @vitest-environment jsdom
import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { DesktopPermissions, LocalSessions, ShareEverySessionLive, providerSetupLine } from "../LocalSessions";
vi.mock("@/lib/analytics", () => ({capture:vi.fn()}));

const auth = vi.hoisted(()=>({userId:"alice",orgId:"org",getToken:vi.fn()}));
const org = vi.hoisted(()=>({organization:{id:"org",name:"Acme"} as {id:string;name:string}|null}));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => auth, useOrganization: () => org }));
const invoke = vi.fn();
beforeEach(() => { invoke.mockReset();auth.getToken.mockReset().mockResolvedValue("jwt"); auth.userId="alice";auth.orgId="org";org.organization={id:"org",name:"Acme"};Object.assign(window,{__TAURI__:{core:{invoke}}}); });
it("explains the full audience and requires an already connected Mac", async () => {
  invoke.mockResolvedValue({enrollment:null,pairedWorkspaceId:null,status:""});
  render(<LocalSessions />);
  await waitFor(()=>expect(screen.getByText("Connect this Mac first.")).toBeTruthy());
  // The text a person ticks "I understand" under states the model: opt in, the workspace, the link apart.
  expect(screen.getByText(/^On: every new Claude Code, Codex, Cursor or Grok CLI session on this Mac is shared with your workspace, from its start\. Every member of your workspace, and its AI agents, can see a shared session’s live terminal and conversation\..* Off: nothing is shared\.$/)).toBeTruthy();
  expect(screen.getByText(/Every member of the workspace where sharing is on, and its AI agents, can see a shared session/)).toBeTruthy();
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
it("a Settings switch the Mac refused to save says so and shows what the Mac still allows", async () => {
  invoke.mockImplementation(async (command) => {
    if (command === "desktop_permissions_set") throw "write desktop_permissions.json: Permission denied";
    return command === "desktop_permissions_get"
      ? {consented:true,shell:true,browser:true,files:false,screen:false,paperclip:true,updatedAt:"1"}
      : undefined;
  });
  render(<DesktopPermissions />);
  const shell = await screen.findByRole("switch", {name:"Shell"});
  await userEvent.click(shell);
  expect(await screen.findByRole("alert")).toHaveTextContent("Permission denied");
  expect(screen.getByRole("switch", {name:"Shell"})).toBeChecked();
  expect(localStorage.getItem("isol8.desktop.permissions.v1")).toBeNull();
});
it("a switch turned on and cancelled on the Mac stays off and says so calmly", async () => {
  invoke.mockImplementation(async (command) => {
    if (command === "desktop_permissions_set") throw "Not changed: declined on this Mac.";
    return command === "desktop_permissions_get"
      ? {consented:true,shell:true,browser:true,files:false,screen:false,paperclip:true,updatedAt:"1"}
      : undefined;
  });
  render(<DesktopPermissions />);
  await userEvent.click(await screen.findByRole("switch", {name:"Files"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Not changed — you chose Cancel on your Mac.");
  expect(screen.getByRole("switch", {name:"Files"})).not.toBeChecked();
  expect(localStorage.getItem("isol8.desktop.permissions.v1")).toBeNull();
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
it("turns Share every new session on and off through the desktop, off by default", async () => {
  invoke.mockImplementation(async (command, args) => command === "shell_integration_set" ? args.enabled : false);
  render(<ShareEverySessionLive />);
  const box = await screen.findByRole("switch", {name:"Share every new session"});
  expect(box).not.toBeChecked();
  await userEvent.click(box);
  await waitFor(() => expect(box).toBeChecked());
  expect(invoke).toHaveBeenCalledWith("shell_integration_set", {enabled:true});
});
it("Settings has one sharing switch: on and off set the shell block with it", async () => {
  let enrolled = false;
  invoke.mockImplementation(async (command, args) => {
    if (command === "local_sessions_set") { enrolled = args.enabled; return 1; }
    if (command === "shell_integration_set") return args.enabled;
    if (command === "shell_integration_status") return enrolled;
    return { enrollment: enrolled ? {ownerId:"org",humanUserId:"alice",enabled:true,paused:false} : null, pairedWorkspaceId:"org", status:"connected" };
  });
  render(<LocalSessions />);
  await userEvent.click(await screen.findByRole("checkbox"));
  await userEvent.click(screen.getByRole("button", {name:"Turn on sharing"}));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("shell_integration_set", {enabled:true}));
  expect(await screen.findByText("On · Acme")).toBeInTheDocument();
  expect(screen.queryByRole("switch", {name:"Share every new session"})).toBeNull();
  await userEvent.click(screen.getByRole("button", {name:"Turn off sharing"}));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("shell_integration_set", {enabled:false}));
});
it("Settings shows Share every new session only where sharing is on and the shell block is not", async () => {
  invoke.mockImplementation(async (command) => command === "shell_integration_status" ? false
    : ({ enrollment:{ownerId:"org",humanUserId:"alice",enabled:true,paused:false}, pairedWorkspaceId:"org", status:"connected" }));
  const view = render(<LocalSessions />);
  expect(await screen.findByRole("switch", {name:"Share every new session"})).not.toBeChecked();
  view.unmount();
  invoke.mockImplementation(async (command) => command === "shell_integration_status" ? false : ({ enrollment:null, pairedWorkspaceId:"org", status:"" }));
  render(<LocalSessions />);
  await screen.findByRole("button", {name:"Turn on sharing"});
  expect(screen.queryByRole("switch", {name:"Share every new session"})).toBeNull();
});

it("names the workspace sharing is on in, never its raw id, and shows the desktop status as its own note", async () => {
  const on = (status: string) => ({enrollment:{ownerId:"org",humanUserId:"alice",enabled:true,paused:false},pairedWorkspaceId:"org",status});
  invoke.mockResolvedValue(on("Setup retried; Claude Code, Codex, Cursor and Grok CLI are configured for new sessions."));
  const view = render(<LocalSessions />);
  expect(await screen.findByText("On · Acme")).toBeInTheDocument();
  expect(screen.getByText("Setup retried; Claude Code, Codex, Cursor and Grok CLI are configured for new sessions.")).toBeInTheDocument();
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
