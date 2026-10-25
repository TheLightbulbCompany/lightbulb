"use client";

import { OrganizationSwitcher, SignIn, useAuth, useOrganization } from "@clerk/nextjs";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiError, useApi } from "@/lib/api";

// `lightbulb login` and `lightbulb invite --agents` open this page with the code their terminal
// shows. Login ties that computer to the signed-in person; --agents (a workspace admin, on a
// computer already theirs) adds the selected workspace's agents to that computer's network.

type Purpose = "login" | "agents";

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 410) return "This code expired. Run the command again.";
    if (err.status === 403) return "Only a workspace admin can add its agents.";
    if (err.status === 503) return "Your workspace is starting. Try again in a moment.";
    const detail = (err.body as { detail?: unknown; message?: unknown } | null) ?? {};
    const said = typeof detail.detail === "string" ? detail.detail : detail.message;
    if (err.status === 409 && typeof said === "string") return said;
  }
  return "Could not confirm. Try again.";
}

function ConfirmComputer() {
  const code = useSearchParams().get("code") ?? "";
  const { isLoaded, isSignedIn } = useAuth();
  const { organization } = useOrganization();
  const api = useApi();
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [error, setError] = useState<string | null>(null);
  // What the computer reported. A phished link names a computer the person does not
  // recognise, which is the point of showing it.
  const [seen, setSeen] = useState<{ purpose: Purpose; computer: string } | null>(null);

  useEffect(() => {
    if (!isSignedIn || !code) return;
    api
      .post("/network/device/describe", { userCode: code })
      .then((r) => setSeen(r as { purpose: Purpose; computer: string }))
      .catch((err) => setError(errorText(err)));
  }, [api, code, isSignedIn]);

  if (isLoaded && !isSignedIn) {
    return (
      <div className="flex justify-center items-center h-screen">
        <SignIn
          forceRedirectUrl={`/auth/network?code=${encodeURIComponent(code)}`}
          appearance={{ elements: { rootBox: "mx-auto" } }}
        />
      </div>
    );
  }

  async function confirm() {
    setState("busy");
    setError(null);
    try {
      await api.post("/network/device/approve", { userCode: code });
      setState("done");
    } catch (err) {
      setError(errorText(err));
      setState("idle");
    }
  }

  const computer = seen?.computer || "this computer";
  const agents = seen?.purpose === "agents";
  let body: React.ReactNode;
  if (!code) {
    body = <p className="text-sm text-muted-foreground">This link has no code. Run the command again.</p>;
  } else if (state === "done") {
    body = (
      <h1 className="text-xl font-semibold">
        {agents
          ? `Done. Allow ${organization?.name ?? "your workspace"} on ${computer} when it asks.`
          : "This computer is signed in. You can close this tab."}
      </h1>
    );
  } else if (!seen) {
    body = error ? <p className="text-sm text-destructive">{error}</p> : null;
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
          {agents ? `Add ${organization?.name}'s agents to ${computer}'s network?` : `Sign in ${computer} as you?`}
        </h1>
        <p className="font-mono text-3xl tracking-widest my-3">{code}</p>
        <p className="text-sm text-muted-foreground mb-4 max-w-sm">
          {agents
            ? "Only continue if you just ran `lightbulb invite --agents` and the code matches what your terminal shows. Signed-in members' computers on that network can then message your agents."
            : "Only continue if you just ran `lightbulb login` on this computer and the code matches what your terminal shows."}
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
