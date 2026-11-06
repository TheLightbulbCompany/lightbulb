"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { OrganizationList, useAuth, useOrganization, useOrganizationList } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { JoinTeamBlock } from "@/components/onboarding/JoinTeamBlock";
import { SourceQuestion, reportSource, type SourceAnswer } from "@/components/onboarding/SourceQuestion";
import { clearWorkspaceDoor, readWorkspaceDoor, touchPayload } from "@/lib/analytics";
import { useNameGate } from "@/hooks/useNameGate";
import { ApiError, useApi } from "@/lib/api";
import { safeReturnTo } from "@/lib/returnTo";

/** Mirrors the backend's `company_identity.slugify`: the availability check rejects any
 *  slug this would change, so the two must agree. */
function slugify(value: string): string {
  const trim = (s: string) => s.replace(/^-+|-+$/g, "");
  return trim(trim(value.normalize("NFKD").replace(/[^\x00-\x7f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-")).slice(0, 32));
}

type SlugCheck = { slug: string; available: boolean; reason: "invalid" | "reserved" | "taken" | null; domain: string };

const SLUG_PROBLEM: Record<NonNullable<SlugCheck["reason"]>, string> = {
  invalid: "Use lowercase letters, numbers and hyphens, up to 32 characters.",
  reserved: "That address is reserved. Try another.",
  taken: "That address is taken. Try another.",
};

/** "Create your workspace": the name, plus the address its agents will email from. The slug
 *  is HELD before the Clerk org exists, so a lost race never leaves a workspace without one;
 *  the organization.created webhook then hands the hold to the workspace. */
function CreateWorkspaceForm() {
  const api = useApi();
  const { createOrganization, setActive } = useOrganizationList();
  const [name, setName] = useState("");
  const [typedSlug, setTypedSlug] = useState<string | null>(null);
  const [check, setCheck] = useState<SlugCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editingSlug, setEditingSlug] = useState(false);
  const [source, setSource] = useState<SourceAnswer>({ answer: "", detail: "" });
  const slug = typedSlug || slugify(name); // clearing the field goes back to the suggestion

  useEffect(() => {
    if (!slug) return;
    let stale = false; // an answer for a slug the user has typed past must not land
    const timer = setTimeout(() => {
      api.get(`/company/slug-availability?slug=${encodeURIComponent(slug)}`)
        .then((r) => { if (!stale) setCheck(r as SlugCheck); })
        .catch(() => { if (!stale) setCheck(null); });
    }, 300);
    return () => { stale = true; clearTimeout(timer); };
  }, [slug, api]);

  const current = check?.slug === slug ? check : null;
  const ready = !!name.trim() && !!current?.available && !busy;

  async function create(event: FormEvent) {
    event.preventDefault();
    if (!ready || !createOrganization || !setActive) return;
    setBusy(true);
    setError(null);
    try {
      // Where they came from, stashed BEFORE the org exists: the organization.created webhook
      // moves it onto the workspace. Never blocks creating the workspace.
      await api
        .post("/company/source", {
          answer: source.answer || null,
          detail: source.detail || null,
          door: readWorkspaceDoor() ?? "sign_up",
          ...touchPayload(),
        })
        .catch(() => undefined);
      await api.post("/company/slug/hold", { slug });
      const org = await createOrganization({ name: name.trim() });
      clearWorkspaceDoor();
      reportSource("sign_up", source, { workspace_id: org.id });
      await setActive({ organization: org.id });
      // Hand the hold to the new workspace now (consistent, keyed); the organization.created
      // webhook repeats it, so a failure here is not the user's problem.
      await api.post("/company/slug/claim", { slug }).catch(() => undefined);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setCheck({ slug, available: false, reason: "taken", domain: current?.domain ?? "" });
      } else {
        setError("We couldn't create your workspace. Please try again.");
      }
      setBusy(false);
    }
  }

  return (
    <form onSubmit={create} className="flex w-full max-w-md flex-col gap-4 rounded-xl border border-border bg-card p-6 shadow-sm">
      <label className="flex flex-col gap-1.5 text-sm font-medium">
        Workspace name
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Workspace name" autoFocus maxLength={64} />
      </label>
      {editingSlug || current?.reason ? (
        <>
          <label className="flex flex-col gap-1.5 text-sm font-medium">
            Agent email address
            <Input
              value={slug}
              onChange={(e) => { setEditingSlug(true); setTypedSlug(e.target.value.toLowerCase()); }}
              placeholder="your-team"
              maxLength={32}
              spellCheck={false}
              autoCapitalize="none"
              aria-describedby="slug-status"
            />
          </label>
          <p id="slug-status" className="-mt-2 text-sm text-muted-foreground" aria-live="polite">
            {!slug ? "Your agents' email address comes from this."
              : current?.reason ? <span className="text-destructive">{SLUG_PROBLEM[current.reason]}</span>
              : current ? <>Lightbulb will email as <span className="font-mono text-foreground">ceo@{slug}.{current.domain}</span></>
              : "Checking…"}
          </p>
        </>
      ) : (
        <p id="slug-status" className="-mt-2 flex items-center gap-2 text-sm text-muted-foreground" aria-live="polite">
          {!slug ? "Your agents' email address comes from the name."
            : current ? <span>Lightbulb will email as <span className="font-mono text-foreground">ceo@{slug}.{current.domain}</span></span>
            : "Checking…"}
          {slug && (
            <button type="button" className="text-foreground underline underline-offset-2" onClick={() => setEditingSlug(true)}>
              Change
            </button>
          )}
        </p>
      )}
      <SourceQuestion value={source} onChange={setSource} />
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <Button type="submit" disabled={!ready} className="self-end">
        {busy ? "Creating…" : "Create workspace"}
      </Button>
    </form>
  );
}

export default function OnboardingPage() {
  const router = useRouter();
  const { isLoaded, isSignedIn, orgId } = useAuth();
  const { organization, isLoaded: orgLoaded } = useOrganization();
  const { userMemberships, userInvitations, isLoaded: listLoaded, setActive } =
    useOrganizationList({ userMemberships: true, userInvitations: true });
  const [joiningOrgId, setJoiningOrgId] = useState<string | null>(null);
  const [accepting, setAccepting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Ask for a name before a workspace exists: `loaded` stays false, so nothing below
  // creates, activates or accepts while the person is on their way to /welcome/name.
  const naming = useNameGate();
  const loaded = !naming && isLoaded && isSignedIn && orgLoaded && listLoaded &&
    userMemberships?.data !== undefined && userInvitations?.data !== undefined &&
    !userMemberships?.isLoading && !userInvitations?.isLoading;
  const invitations = userInvitations?.data ?? [];
  const count = userMemberships?.data?.length ?? 0;
  const soleOrgId = loaded && !invitations.length && !organization && !joiningOrgId && count === 1
    ? userMemberships?.data?.[0]?.organization.id : undefined;

  useEffect(() => {
    if (!soleOrgId || !setActive || error) return;
    void setActive({ organization: soleOrgId }).catch(() => {
      setError("We couldn't activate your workspace. Please try again.");
    });
  }, [soleOrgId, setActive, error]);

  useEffect(() => {
    if (!loaded || invitations.length || !organization || orgId !== organization.id) return;
    if (joiningOrgId && organization.id !== joiningOrgId) return;
    router.replace(safeReturnTo(new URLSearchParams(window.location.search).get("returnTo")));
  }, [loaded, invitations.length, organization, orgId, joiningOrgId, router]);

  async function activateJoinedWorkspace(id: string) {
    await userInvitations?.revalidate?.();
    await userMemberships?.revalidate?.();
    await setActive?.({ organization: id });
  }

  async function retryActivation() {
    setError(null);
    if (!joiningOrgId) return;
    try {
      await activateJoinedWorkspace(joiningOrgId);
    } catch {
      setError("We couldn't activate your workspace. Please try again.");
    }
  }

  async function acceptInvitation(invitation: (typeof invitations)[number]) {
    setJoiningOrgId(invitation.publicOrganizationData.id);
    setAccepting(true);
    setError(null);
    let accepted = false;
    try {
      await invitation.accept();
      accepted = true;
      await activateJoinedWorkspace(invitation.publicOrganizationData.id);
    } catch {
      if (!accepted) setJoiningOrgId(null);
      setError("We couldn't join your workspace. Please try again.");
    } finally {
      setAccepting(false);
    }
  }

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-6 bg-background p-4">
      {error && <p role="alert">{error}</p>}
      {!loaded ? <p role="status">Loading your workspaces…</p> : invitations.length > 0 ? (
        <>
          <h1 className="text-2xl font-bold">You have been invited</h1>
          <p>Accept an invitation to join a workspace on Lightbulb.</p>
          <div className="flex w-full max-w-md flex-col gap-3">
            {invitations.map((invitation) => (
              <div key={invitation.id} className="flex items-center justify-between gap-4 rounded-lg border border-border bg-card p-4">
                <span>{invitation.publicOrganizationData.name || "Workspace"}</span>
                <Button onClick={() => acceptInvitation(invitation)} disabled={accepting}>
                  {accepting && joiningOrgId === invitation.publicOrganizationData.id ? "Accepting…" : "Accept"}
                </Button>
              </div>
            ))}
          </div>
        </>
      ) : joiningOrgId || organization || count === 1 ? (
        <>
          <p role="status">Activating your workspace…</p>
          {error && <Button onClick={retryActivation}>Try again</Button>}
        </>
      ) : count > 1 ? (
        <>
          <h1 className="text-2xl font-bold">Choose your workspace</h1>
          <OrganizationList hidePersonal afterSelectOrganizationUrl="/onboarding" afterCreateOrganizationUrl="/onboarding" skipInvitationScreen />
        </>
      ) : (
        <>
          <h1 className="text-2xl font-bold">Create your workspace</h1>
          <p>Your workspace brings your agents, work, and billing together.</p>
          <CreateWorkspaceForm />
          <JoinTeamBlock />
        </>
      )}
    </main>
  );
}
