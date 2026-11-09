"use client";

// The one rail on web (bottom tabs on a phone), top to bottom (founder, 8 Nov 2026): the Lightbulb logo → the orb and
// the assistant's room (/voice/lightbulb, where /chat lands now) · Home · Agent · Needs you · Settings.
// Settings is /voice/settings: the existing Settings panels, rendered here beside the rail.
import "./voice.css";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { OrganizationList, UserButton, useAuth, useOrganization } from "@clerk/nextjs";
import { CircleAlert, Home, PhoneCall, Settings } from "lucide-react";
import { LogoMark } from "@/components/Logo";
import { HuddleProvider } from "@/components/huddle/HuddleProvider";
import { LIGHTBULB as ASSISTANT_NAME, useQuestions } from "./data";
import { TalkSessionProvider } from "./TalkSessionProvider";

export function useNeedsYouTotal(): number {
  return useQuestions().data?.length ?? 0;
}

const LIGHTBULB = "/voice/lightbulb";
const PLACES = [
  { href: "/voice", label: "Home", Icon: Home },
  { href: "/voice/agent", label: "Agent", Icon: PhoneCall },
  { href: "/voice/needs-you", label: "Needs you", Icon: CircleAlert },
  { href: "/voice/settings", label: "Settings", Icon: Settings },
] as const;

export function VoiceShell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const { userId } = useAuth();
  const { organization, isLoaded } = useOrganization();
  const needs = useNeedsYouTotal();
  if (isLoaded && !organization) {
    // Clerk <OrganizationList> props: https://clerk.com/docs/components/organization/organization-list
    return <main className="vx-page"><h1 className="vx-title">Choose your workspace</h1>
      <div className="vx-orgs"><OrganizationList hidePersonal afterSelectOrganizationUrl="/voice" afterCreateOrganizationUrl="/onboarding" skipInvitationScreen /></div></main>;
  }
  return (
    <HuddleProvider key={`${userId ?? "loading"}:${organization?.id ?? "loading"}`}>
      <TalkSessionProvider name={ASSISTANT_NAME}>
      <div className="vx-shell">
        <nav className="vx-rail" aria-label="Places">
          <Link href={LIGHTBULB} className="vx-logo" aria-label="Lightbulb" aria-current={path === LIGHTBULB ? "page" : undefined}><LogoMark /></Link>
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
      </TalkSessionProvider>
    </HuddleProvider>
  );
}
