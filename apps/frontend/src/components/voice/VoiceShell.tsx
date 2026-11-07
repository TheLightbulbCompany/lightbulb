"use client";

// The four places on web (rail) and phone (bottom tabs): Home · Needs you · Agent · Settings (spec 2026-11-07 §5).
// Settings is the existing Settings place, opened inside the workspace shell.
import "./voice.css";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { UserButton, useOrganization } from "@clerk/nextjs";
import { CircleAlert, Home, PhoneCall, Settings } from "lucide-react";
import { HuddleProvider } from "@/components/huddle/HuddleProvider";
import { callNeedsYou, useQuestions, useTimeline } from "./data";

export function useNeedsYouTotal(): number {
  const { data: qs } = useQuestions();
  const { data: items } = useTimeline();
  return (qs?.length ?? 0) + (items?.filter(callNeedsYou).length ?? 0);
}

const PLACES = [
  { href: "/voice", label: "Home", Icon: Home },
  { href: "/voice/needs-you", label: "Needs you", Icon: CircleAlert },
  { href: "/voice/agent", label: "Agent", Icon: PhoneCall },
  { href: "/chat?panel=settings", label: "Settings", Icon: Settings },
] as const;

export function VoiceShell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const { organization, isLoaded } = useOrganization();
  const needs = useNeedsYouTotal();
  if (isLoaded && !organization) {
    return <main className="vx-page"><h1 className="vx-title">Choose a workspace first</h1>
      <p className="vx-sub"><Link className="vx-link" href="/chat">Open Lightbulb</Link> and pick your business.</p></main>;
  }
  return (
    <HuddleProvider>
      <div className="vx-shell">
        <nav className="vx-rail" aria-label="Places">
          {PLACES.map(({ href, label, Icon }) => {
            const badge = href === "/voice/needs-you" && needs > 0;
            return <Link key={href} href={href} aria-current={path === href ? "page" : undefined}
              aria-label={badge ? `${label}, ${needs}` : undefined}>
              <Icon size={20} aria-hidden="true" />{label}
              {badge && <span className="vx-badge" aria-hidden="true">{needs > 9 ? "9+" : needs}</span>}
            </Link>;
          })}
          <div className="vx-spacer" />
          <div className="vx-account"><UserButton appearance={{ elements: { avatarBox: "h-8 w-8" } }} /></div>
        </nav>
        <main className="vx-main">{children}</main>
      </div>
    </HuddleProvider>
  );
}
