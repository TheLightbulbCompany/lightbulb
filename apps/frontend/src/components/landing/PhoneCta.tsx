"use client";

import { PHONE_VOICES, voiceSamplePath } from "@lightbulb/shared";
import Link from "next/link";
import { useEffect, useRef } from "react";

import { captureSignupCta } from "@/lib/signupFlow";

const LABEL = "Set up your assistant";

/** /phone's sign-up door, tracked like every other sign-up button. */
export function SignupLink({ className, ctaId, surface }: { className: string; ctaId: string; surface: string }) {
  return (
    <Link
      href="/sign-up"
      className={className}
      onClick={() => captureSignupCta({ ctaId, label: LABEL, surface, destination: "/sign-up" })}
    >
      {LABEL}
    </Link>
  );
}

/**
 * The stock samples in /public/voice-samples, one per voice. A click plays it in place (again stops it);
 * without JavaScript the link opens the file. PhonePlayer lights the first row and its bars.
 */
export function VoiceSamples() {
  const audio = useRef<HTMLAudioElement | null>(null);
  useEffect(() => () => audio.current?.pause(), []);
  const play = (e: React.MouseEvent<HTMLAnchorElement>) => {
    e.preventDefault();
    const a = (audio.current ??= new Audio());
    const same = a.src === e.currentTarget.href && !a.paused;
    a.pause();
    if (same) return;
    a.src = e.currentTarget.href;
    void a.play().catch(() => {});
  };
  return PHONE_VOICES.map((v, i) => {
    const name = v.label.replace(" (default)", "");
    return (
      <div className="vc" id={i === 0 ? "v0" : undefined} key={v.id}>
        <a href={voiceSamplePath(v.id, "en")} onClick={play} aria-label={`Play ${name}, a stock sample`}>
          <span className="pl">
            <svg viewBox="0 0 12 12" aria-hidden="true">
              <path d="M3 1.5v9l7.5-4.5z" fill="#0F1A2B" />
            </svg>
          </span>
          {name}
        </a>
        {i === 0 && (
          <span className="bars" aria-hidden="true">
            {[0, 1, 2, 3, 4, 5, 6].map((j) => (
              <b key={j} />
            ))}
          </span>
        )}
      </div>
    );
  });
}
