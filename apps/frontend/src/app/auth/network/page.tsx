"use client";

import { OrganizationSwitcher, SignIn, useAuth, useOrganization } from "@clerk/nextjs";
import { Suspense, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, useApi } from "@/lib/api";

// `lightbulb login` and `lightbulb invite --agents` open this page; the person types the code
// their terminal shows (the link carries none, so a link alone confirms nothing). Login ties
// that computer to the signed-in person (a free account needs no workspace) and to the selected
// workspace, the one its workspace tools act in; --agents (a
// workspace admin, on a computer already theirs) adds the selected workspace's agents to that
// computer's network.

type Purpose = "login" | "agents";
type Seen = { purpose: Purpose; computer: string; startedAt?: number | null; ip?: string };

const PAGE = "/auth/network";
const RETRY_MS = 15000; // between approve retries while a workspace starts

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 410) return "No such code, or it expired. Check your terminal, or run the command again.";
    if (err.status === 403) return "Only a workspace admin can add its agents.";
    if (err.status === 503 || err.status === 504) return "Your workspace is starting. Try again in a moment.";
    const detail = (err.body as { detail?: unknown; message?: unknown } | null) ?? {};
    const said = typeof detail.detail === "string" ? detail.detail : detail.message;
    if (err.status === 409 && typeof said === "string") return said;
  }
  return "Could not confirm. Try again.";
}

function ago(startedAt: number | null | undefined): string {
  if (!startedAt) return "just now";
  const s = Math.max(0, Math.round(Date.now() / 1000 - startedAt));
  return s < 90 ? `${s} seconds ago` : `${Math.round(s / 60)} minutes ago`;
}

function ConfirmComputer() {
  const { isLoaded, isSignedIn } = useAuth();
  const { organization } = useOrganization();
  const api = useApi();
  const [typed, setTyped] = useState("");
  const [code, setCode] = useState("");
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [error, setError] = useState<string | null>(null);
  // What the computer reported. Its name is whatever it chose; where and when it asked are
  // ours, so a request the person did not make stands out.
  const [seen, setSeen] = useState<Seen | null>(null);

  if (isLoaded && !isSignedIn) {
    return (
      <div className="flex justify-center items-center h-screen">
        <SignIn
          forceRedirectUrl={PAGE}
          signUpForceRedirectUrl={PAGE}
          appearance={{ elements: { rootBox: "mx-auto" } }}
        />
      </div>
    );
  }

  async function lookUp(e: React.FormEvent) {
    e.preventDefault();
    const c = typed.trim().toUpperCase();
    if (!c) return;
    setError(null);
    try {
      setSeen((await api.post("/network/device/describe", { userCode: c })) as Seen);
      setCode(c);
    } catch (err) {
      setError(errorText(err));
    }
  }

  async function confirm() {
    setState("busy");
    setError(null);
    // A cold workspace can outlast the gateway's 30 s: keep asking (a retry of the same code
    // joins once), for up to two minutes.
    for (let i = 0; i < 8; i++) {
      try {
        await api.post("/network/device/approve", { userCode: code });
        setState("done");
        return;
      } catch (err) {
        const starting = err instanceof ApiError && (err.status === 503 || err.status === 504);
        setError(errorText(err));
        if (!starting) break;
        await new Promise((r) => setTimeout(r, RETRY_MS));
      }
    }
    setState("idle");
  }

  const computer = seen?.computer || "this computer";
  const agents = seen?.purpose === "agents";
  let body: React.ReactNode;
  if (state === "done") {
    body = (
      <h1 className="text-xl font-semibold">
        {agents
          ? `Done. ${organization?.name ?? "Your workspace"} is joining the network on ${computer}.`
          : "This computer is signed in. You can close this tab."}
      </h1>
    );
  } else if (!seen) {
    body = (
      <form onSubmit={lookUp} className="flex flex-col items-center gap-3">
        <h1 className="text-xl font-semibold">Type the code your terminal shows</h1>
        <p className="text-sm text-muted-foreground max-w-sm">
          Only if you just ran `lightbulb login` or `lightbulb invite --agents` yourself.
        </p>
        <Input
          aria-label="Code"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder="ABCD-EFGH"
          autoComplete="off"
          className="font-mono text-center text-xl tracking-widest w-56"
        />
        <Button type="submit">Continue</Button>
        {error && <p className="text-sm text-destructive">{error}</p>}
      </form>
    );
  } else if (agents && !organization) {
    body = (
      <>
        <h1 className="text-xl font-semibold mb-2">Choose the workspace whose agents to add</h1>
        <OrganizationSwitcher hidePersonal />
      </>
    );
  } else {
    body = (
      <>
        <h1 className="text-xl font-semibold mb-2">
          {agents
            ? `Add ${organization?.name}'s agents to ${computer}'s network?`
            : `Sign in ${computer} as you${organization ? `, for ${organization.name}` : ""}?`}
        </h1>
        <p className="text-sm text-muted-foreground mb-4 max-w-sm">
          {`Requested ${ago(seen.startedAt)}${seen.ip ? ` from ${seen.ip}` : ""}. `}
          {agents
            ? "Signed-in members' computers on that network can then message your agents."
            : "Only continue if this is your computer and you just asked."}
        </p>
        <Button onClick={confirm} disabled={state === "busy"}>
          {agents ? "Add the agents" : "Sign in this computer"}
        </Button>
        {error && <p className="text-sm text-destructive mt-3">{error}</p>}
      </>
    );
  }

  return (
    <div className="flex justify-center items-center h-screen">
      <div className="text-center flex flex-col items-center">{body}</div>
    </div>
  );
}

export default function NetworkConfirmPage() {
  return (
    <Suspense>
      <ConfirmComputer />
    </Suspense>
  );
}
