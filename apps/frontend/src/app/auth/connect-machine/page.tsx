"use client";

import { OrganizationSwitcher, SignIn, useAuth, useOrganization } from "@clerk/nextjs";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiError, useApi } from "@/lib/api";

// `lightbulb join` opens this page with the code it shows in the terminal. Approving pairs
// and enrolls that Mac server-side as the signed-in person, in their selected workspace.

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 410) return "This code expired. Run `lightbulb join` again.";
    if (err.status === 409) {
      const detail = (err.body as { detail?: { code?: string } } | null)?.detail;
      if (detail?.code === "workspace_required") return "Choose a workspace first.";
      return "Someone else already connected this Mac. Run `lightbulb join` again.";
    }
  }
  return "Could not connect this Mac. Try again.";
}

function ConnectMachine() {
  const code = useSearchParams().get("code") ?? "";
  const { isLoaded, isSignedIn } = useAuth();
  const { organization } = useOrganization();
  const api = useApi();
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [error, setError] = useState<string | null>(null);

  if (isLoaded && !isSignedIn) {
    return (
      <div className="flex justify-center items-center h-screen">
        <SignIn
          forceRedirectUrl={`/auth/connect-machine?code=${encodeURIComponent(code)}`}
          appearance={{ elements: { rootBox: "mx-auto" } }}
        />
      </div>
    );
  }

  async function approve() {
    setState("busy");
    setError(null);
    try {
      await api.post("/auth/device/approve", { userCode: code });
      setState("done");
    } catch (err) {
      setError(errorText(err));
      setState("idle");
    }
  }

  let body: React.ReactNode;
  if (!code) {
    body = <p className="text-sm text-muted-foreground">This link has no code. Run `lightbulb join` again.</p>;
  } else if (state === "done") {
    body = <h1 className="text-xl font-semibold">Connected. You can close this tab.</h1>;
  } else if (!organization) {
    body = (
      <>
        <h1 className="text-xl font-semibold mb-2">Choose a workspace for this Mac</h1>
        <OrganizationSwitcher hidePersonal />
      </>
    );
  } else {
    body = (
      <>
        <h1 className="text-xl font-semibold mb-2">Connect this Mac to {organization.name}</h1>
        <p className="text-sm text-muted-foreground mb-1">Check that your terminal shows this code:</p>
        <p className="font-mono text-2xl tracking-widest mb-4">{code}</p>
        <Button onClick={approve} disabled={state === "busy"}>
          Connect this Mac
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

export default function ConnectMachinePage() {
  return (
    <Suspense>
      <ConnectMachine />
    </Suspense>
  );
}
