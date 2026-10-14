"use client";

import { useState, type ReactNode } from "react";
import { JoinTeamBlock } from "@/components/onboarding/JoinTeamBlock";

/** Two doors above the form (10 Section C): start a workspace, or join the team that already has one.
 *  The first door is the Clerk form the page passes in; the second is the join-link paste box. */
export function SignUpDoors({ createForm }: { createForm: ReactNode }) {
  const [door, setDoor] = useState<"create" | "join">("create");
  return (
    <>
      <div className="auth-doors" role="tablist" aria-label="How are you getting started?">
        <button type="button" role="tab" aria-selected={door === "create"} className="auth-door" onClick={() => setDoor("create")}>
          <span className="auth-door-title">Create a workspace</span>
          <span className="auth-door-body">For the person setting up the team. 30 days free, no card.</span>
        </button>
        <button type="button" role="tab" aria-selected={door === "join"} className="auth-door" onClick={() => setDoor("join")}>
          <span className="auth-door-title">Join your team</span>
          <span className="auth-door-body">Got an invite? Open the email on this device. Have a join link? Paste it here.</span>
        </button>
      </div>
      {door === "create" ? createForm : <JoinTeamBlock compact />}
    </>
  );
}
