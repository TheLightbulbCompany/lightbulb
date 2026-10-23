"use client";

import { OrganizationSwitcher, SignIn, useAuth, useOrganization } from "@clerk/nextjs";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

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
  // The name the Mac reported at `lightbulb join`. A phished link names a Mac the person
  // does not recognise, which is the point of showing it.
  const [machineName, setMachineName] = useState<string | null>(null);

  useEffect(() => {
    if (!isSignedIn || !code || !organization) return;
    api
      .post("/auth/device/describe", { userCode: code })
      .then((r) => setMachineName((r as { machineName?: string }).machineName || "this Mac"))
      .catch((err) => setError(errorText(err)));
  }, [api, code, isSignedIn, organization]);

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
        <h1 className="text-xl font-semibold mb-2">
          Connect {machineName ?? "this Mac"} to {organization.name}?
        </h1>
        <p className="font-mono text-3xl tracking-widest my-3">{code}</p>
        <p className="text-sm text-muted-foreground mb-4 max-w-sm">
          Only continue if you just ran `lightbulb join` on this Mac and the code matches what your
          terminal shows.
        </p>
        <Button onClick={approve} disabled={state === "busy" || machineName === null}>
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

// `mode=network` (the `/device` page): a Lightbulb Connect computer asks the signed-in person
// to vouch for their name. The person TYPES the code their terminal shows; nothing in the
// URL fills it in, so a phished link cannot carry one. No workspace is needed.
function networkErrorText(err: unknown): string {
  if (err instanceof ApiError && err.status === 410) return "This code is wrong or expired. Start again on your computer.";
  if (err instanceof ApiError && err.status === 409) return "Someone else already approved this code.";
  return "Could not sign in this computer. Try again.";
}

function NetworkSignIn() {
  const { isLoaded, isSignedIn } = useAuth();
  const api = useApi();
  const [code, setCode] = useState("");
  const [computer, setComputer] = useState<{ computer_name: string; key_short: string } | null>(null);
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [error, setError] = useState<string | null>(null);

  if (isLoaded && !isSignedIn) {
    return (
      <div className="flex justify-center items-center h-screen">
        <SignIn forceRedirectUrl="/auth/connect-machine?mode=network" appearance={{ elements: { rootBox: "mx-auto" } }} />
      </div>
    );
  }

  async function call(path: string, then: (r: unknown) => void) {
    setState("busy");
    setError(null);
    try {
      then(await api.post(path, { user_code: code.trim() }));
    } catch (err) {
      setError(networkErrorText(err));
      setState("idle");
    }
  }

  let body: React.ReactNode;
  if (state === "done") {
    body = <h1 className="text-xl font-semibold">Done. You can close this tab.</h1>;
  } else if (!computer) {
    body = (
      <form
        className="flex flex-col items-center"
        onSubmit={(e) => {
          e.preventDefault();
          void call("/network/device/describe", (r) => {
            setComputer(r as { computer_name: string; key_short: string });
            setState("idle");
          });
        }}
      >
        <label htmlFor="network-code" className="text-xl font-semibold mb-3">
          Enter the code your computer shows
        </label>
        <input
          id="network-code"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          autoComplete="off"
          className="font-mono text-2xl tracking-widest text-center border rounded px-3 py-2 mb-4 bg-transparent"
        />
        <Button type="submit" disabled={state === "busy" || !code.trim()}>
          Continue
        </Button>
      </form>
    );
  } else {
    body = (
      <>
        <h1 className="text-xl font-semibold mb-2">Sign in {computer.computer_name || "this computer"}?</h1>
        <p className="font-mono text-sm text-muted-foreground mb-4">{computer.key_short}</p>
        <Button onClick={() => void call("/network/device/approve", () => setState("done"))} disabled={state === "busy"}>
          Sign in
        </Button>
      </>
    );
  }

  return (
    <div className="flex justify-center items-center h-screen">
      <div className="text-center flex flex-col items-center max-w-sm">
        {state !== "done" && (
          <p className="text-sm text-muted-foreground mb-4">
            Sign in to verify your name on the Lightbulb network. This does not create a workspace or start a plan.
          </p>
        )}
        {body}
        {error && <p className="text-sm text-destructive mt-3">{error}</p>}
      </div>
    </div>
  );
}

function Page() {
  return useSearchParams().get("mode") === "network" ? <NetworkSignIn /> : <ConnectMachine />;
}

export default function ConnectMachinePage() {
  return (
    <Suspense>
      <Page />
    </Suspense>
  );
}
