"use client";

// The one rail on web (bottom tabs on a phone), top to bottom (founder, 8 Nov 2026): the Lightbulb logo → the orb and
// the assistant's room (/voice/lightbulb, where /chat lands now) · Home · Phone (the receptionist's settings, /voice/agent) ·
// Meetings · Needs you · Settings.
// Settings is /voice/settings: the existing Settings panels, rendered here beside the rail.
import "./voice.css";
import Link from "next/link";
import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { UserButton, useAuth } from "@clerk/nextjs";
import { CircleAlert, Home, PhoneCall, Settings, Video } from "lucide-react";
import { LogoMark } from "@/components/Logo";
import { HuddleProvider } from "@/components/huddle/HuddleProvider";
import { notify, notifySuccess } from "@/lib/notify";
import { LIGHTBULB as ASSISTANT_NAME, needsOnboarding, useAgentConfig, useGoLiveAfterPayment, useProvisionOnOpen, useQuestions } from "./data";
import { TalkSessionProvider } from "./TalkSessionProvider";

export function useNeedsYouTotal(): number {
  return useQuestions().data?.length ?? 0;
}

const LIGHTBULB = "/voice/lightbulb";
const PLACES = [
  { href: "/voice", label: "Home", Icon: Home },
  { href: "/voice/agent", label: "Phone", Icon: PhoneCall },
  { href: "/voice/meetings", label: "Meetings", Icon: Video },
  { href: "/voice/needs-you", label: "Needs you", Icon: CircleAlert },
  { href: "/voice/settings", label: "Settings", Icon: Settings },
] as const;
// Until setup is finished, every other page sends the owner to Home (the five steps). Settings stays open: paying and
// connecting Google happen there mid-setup.
const OPEN_DURING_SETUP = new Set(["/voice", "/voice/settings", "/voice/setup"]);

export function VoiceShell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const { userId } = useAuth();
  const needs = useNeedsYouTotal();
  useProvisionOnOpen();
  useGoLiveAfterPayment();
  const router = useRouter();
  const { data: config, isNew } = useAgentConfig();
  // The signed-in person owns their account: with no business saved they name it first (/onboarding), then every
  // page but Home and Settings waits until setup is finished.
  const noBusiness = !!config && !config.live && !config.business?.trim();
  const setupFirst = noBusiness || (needsOnboarding(config, isNew) && !OPEN_DURING_SETUP.has(path));
  useEffect(() => { if (setupFirst) router.replace(noBusiness ? "/onboarding" : "/voice"); }, [setupFirst, noBusiness, router]);
  // Stripe returns here with ?voice= (core/billing/voice_plan_checkout.py success_url / cancel_url).
  useEffect(() => {
    const flag = new URLSearchParams(window.location.search).get("voice");
    // After this tick: a Stripe return is a full page load, and the root Toaster (a later sibling) has not
    // subscribed yet when this effect runs. sonner drops a toast fired before its Toaster subscribes.
    const later = setTimeout(() => {
      if (flag === "checkout_complete") notifySuccess("Payment received. Your assistant is going live.", { dedupeKey: "voice-checkout" });
      else if (flag === "checkout_canceled") notify("Checkout cancelled. You have not been charged.", { dedupeKey: "voice-checkout" });
    });
    return () => clearTimeout(later);
  }, []);
  return (
    <HuddleProvider key={userId ?? "loading"}>
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
        <main className="vx-main">{setupFirst ? null : children}</main>
      </div>
      </TalkSessionProvider>
    </HuddleProvider>
  );
}
