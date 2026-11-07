"use client";

// The four places on web (rail) and phone (bottom tabs): Home · Needs you · Agent · Settings (spec 2026-11-07 §5).
// Settings is /voice/settings: the existing Settings panels, rendered here beside the rail.
import "./voice.css";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { OrganizationList, UserButton, useOrganization } from "@clerk/nextjs";
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
  { href: "/voice/settings", label: "Settings", Icon: Settings },
] as const;

export function VoiceShell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const { organization, isLoaded } = useOrganization();
  const needs = useNeedsYouTotal();
  if (isLoaded && !organization) {
    // Clerk <OrganizationList> props: https://clerk.com/docs/components/organization/organization-list
    return <main className="vx-page"><h1 className="vx-title">Choose your workspace</h1>
      <div className="vx-orgs"><OrganizationList hidePersonal afterSelectOrganizationUrl="/voice" afterCreateOrganizationUrl="/onboarding" skipInvitationScreen /></div></main>;
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
