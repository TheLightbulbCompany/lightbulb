"use client";

import { useState } from "react";
import { Mail, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useApi } from "@/lib/api";

type Role = "org:admin" | "org:member";

interface SentInvite {
  email: string;
  role: Role;
  invitation_id: string;
}

export function InviteTeammatesStep({
  orgId,
  onComplete,
}: {
  orgId: string;
  onComplete: () => void;
}) {
  const api = useApi();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("org:member");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<SentInvite[]>([]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = (await api.post(`/orgs/${orgId}/invitations`, {
        email: email.trim(),
        role,
      })) as { invitation_id: string };
      setSent((prev) => [
        ...prev,
        { email: email.trim(), role, invitation_id: result.invitation_id },
      ]);
      setEmail("");
    } catch (err: unknown) {
      // useApi throws an ApiError with `.status` and `.body`. The backend
      // surfaces error reasons at `body.detail`, which can take TWO shapes:
      //
      //   (a) Object: `{detail: {code, message, ...}}` — used by the
      //       structured 409s (PERSONAL_USER_EXISTS, PENDING_ORG_INVITATION).
      //   (b) String: `{detail: "duplicate invitation"}` — used by Clerk
      //       errors that the backend forwards verbatim via
      //       `HTTPException(detail=response.text)` (clerk_admin.py).
      //
      // Both shapes carry actionable info for the org admin. Falling back
      // to the generic message hides Clerk's "already invited" / "invalid
      // role" reasons and forces guesswork. Handle both before falling
      // through.
      const apiErr = err as {
        status?: number;
        body?: { detail?: string | { message?: string } };
      };
      const detail = apiErr.body?.detail;
      let resolved: string | undefined;
      if (typeof detail === "string") {
        resolved = detail;
      } else if (detail && typeof detail.message === "string") {
        resolved = detail.message;
      }
      const msg =
        resolved && resolved.length > 0
          ? resolved
          : "Failed to send invitation. Please try again.";
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col items-center justify-center min-h-screen gap-6 bg-background">
      <div className="text-center">
        <Mail className="h-10 w-10 mx-auto mb-3 text-primary" />
        <h1 className="text-2xl font-bold">Invite your teammates</h1>
        <p className="text-muted-foreground mt-2">
          They&apos;ll get an email to join your organization on Lightbulb.
        </p>
      </div>

      <form
        onSubmit={handleSubmit}
        className="flex flex-col gap-3 w-full max-w-md px-4"
      >
        <div>
          <label htmlFor="invite-email" className="block text-sm font-medium mb-1">
            Email
          </label>
          <Input
            id="invite-email"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="teammate@example.com"
            disabled={submitting}
          />
        </div>
        <div>
          <label htmlFor="invite-role" className="block text-sm font-medium mb-1">
            Role
          </label>
          <select
            id="invite-role"
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
            disabled={submitting}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
          >
            <option value="org:member">Member</option>
            <option value="org:admin">Admin</option>
          </select>
        </div>
        {error && (
          <div
            role="alert"
            className="rounded-lg border border-err bg-cream px-3 py-2 text-ui text-err"
          >
            {error}
          </div>
        )}
        <Button type="submit" disabled={submitting || !email.trim()}>
          <Plus className="mr-1 h-4 w-4" />
          {submitting ? "Sending..." : "Send invite"}
        </Button>
      </form>

      {sent.length > 0 && (
        <ul className="w-full max-w-md px-4 space-y-2">
          {sent.map((s) => (
            <li
              key={s.invitation_id}
              className="rounded-md border border-border bg-card px-3 py-2 text-sm"
            >
              Invited <strong>{s.email}</strong>{" "}
              <span className="text-muted-foreground">
                ({s.role.replace("org:", "")})
              </span>
            </li>
          ))}
        </ul>
      )}

      <Button type="button" variant="ghost" onClick={onComplete}>
        Done
      </Button>
    </div>
  );
}
