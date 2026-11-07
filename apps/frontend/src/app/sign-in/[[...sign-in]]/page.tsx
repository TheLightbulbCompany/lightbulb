"use client";

import { SignIn } from "@clerk/nextjs";
import Link from "next/link";
import { useEffect, useState } from "react";

import { Logo } from "@/components/Logo";

const TASKS = [
  "your report",
  "meeting notes",
  "inbox triage",
  "code review",
  "data analysis",
];

const ROTATE_INTERVAL = 2800;

export default function Page() {
  const [currentIndex, setCurrentIndex] = useState(0);
  const [exitIndex, setExitIndex] = useState<number | null>(null);

  useEffect(() => {
    const prefersReduced = window.matchMedia(
      "(prefers-reduced-motion: reduce)"
    ).matches;
    if (prefersReduced) return;

    const interval = setInterval(() => {
      setCurrentIndex((prev) => {
        setExitIndex(prev);
        return (prev + 1) % TASKS.length;
      });
      setTimeout(() => {
        setExitIndex(null);
      }, 500);
    }, ROTATE_INTERVAL);

    return () => clearInterval(interval);
  }, []);



  return (
    <>
      <style>{`
        /* ── LAYOUT: split screen ── */
        .auth-layout {
          display: grid;
          grid-template-columns: 1fr 1fr;
          min-height: 100vh;
        }

        /* ── LEFT PANEL: branding ── */
        /* The brand panel is the dark half in both themes. On the dark ladder --midnight is
           the light text colour, so re-point the two names it uses instead of inverting. */
        :root[data-chat-theme="dark"] .auth-brand {
          --midnight: var(--plane-rail);
          --cream: var(--fg);
        }

        .auth-brand {
          background: var(--midnight);
          display: flex;
          flex-direction: column;
          justify-content: space-between;
          padding: 48px;
          position: relative;
          overflow: hidden;
        }
        .auth-brand-top {
          position: relative;
          z-index: 1;
        }
        .auth-brand-middle {
          position: relative;
          z-index: 1;
          flex: 1;
          display: flex;
          flex-direction: column;
          justify-content: center;
        }
        .auth-brand-welcome {
          font-family: var(--font-sans), system-ui, sans-serif;
          font-size: 14px;
          font-weight: 500;
          color: var(--brass);
          margin-bottom: 16px;
          letter-spacing: .5px;
          text-transform: uppercase;
        }
        .auth-brand-tagline {
          font-family: var(--font-serif), Georgia, serif;
          font-size: clamp(32px, 3.5vw, 48px);
          font-weight: 400;
          line-height: 1.2;
          letter-spacing: -.02em;
          color: var(--cream);
          margin: 0;
        }

        /* rotating task words */
        .auth-task-rotate {
          display: inline-block;
          position: relative;
          font-style: italic;
          color: var(--cream);
          height: 1.4em;
          vertical-align: bottom;
          overflow: hidden;
          padding-bottom: 0.1em;
        }
        .auth-task-word {
          display: inline-block;
          position: absolute;
          left: 0;
          top: 0;
          white-space: nowrap;
          opacity: 0;
          transform: translateY(100%);
          transition: opacity 450ms ease-out, transform 450ms ease-out;
        }
        .auth-task-word--active {
          position: relative;
          opacity: 1;
          transform: translateY(0);
        }
        .auth-task-word--exit {
          opacity: 0;
          transform: translateY(-100%);
        }

        .auth-brand-bottom {
          position: relative;
          z-index: 1;
        }

        /* ── RIGHT PANEL: sign-in form ── */
        .auth-form-panel {
          background: var(--cream);
          display: flex;
          align-items: center;
          justify-content: center;
          padding: 48px;
          position: relative;
        }
        .auth-form-panel::before {
          content: '';
          position: absolute;
          top: 60px;
          right: 60px;
          width: 200px;
          height: 200px;
          background: color-mix(in srgb, var(--midnight) 3%, transparent);
          border-radius: 50%;
          filter: blur(60px);
          pointer-events: none;
        }

        .auth-card {
          width: 100%;
          max-width: 400px;
          position: relative;
          z-index: 1;
        }

        .auth-header {
          margin-bottom: 32px;
        }
        .auth-title {
          font-family: var(--font-serif), Georgia, serif;
          font-size: 28px;
          font-weight: 400;
          color: var(--midnight);
          margin: 0 0 8px 0;
        }
        .auth-subtitle {
          font-family: var(--font-sans), system-ui, sans-serif;
          font-size: 14px;
          color: var(--midnight-muted);
          line-height: 1.5;
          margin: 0;
        }

        /* footer link */
        .auth-footer {
          text-align: center;
          font-family: var(--font-sans), system-ui, sans-serif;
          font-size: 14px;
          color: var(--midnight-muted);
          margin-top: 24px;
        }
        .auth-footer a {
          color: var(--midnight);
          font-weight: 600;
          padding: 4px 2px;
          text-decoration: none;
          transition: color 200ms ease-out;
        }
        .auth-footer a:hover {
          color: var(--midnight-soft);
        }
        .auth-footer a:focus-visible {
          outline: 2px solid var(--midnight);
          outline-offset: 2px;
          border-radius: 4px;
        }

        /* ── MOBILE LOGO ── */
        .auth-mobile-logo {
          display: none;
          margin-bottom: 32px;
        }

        /* ── MOBILE ── */
        @media (max-width: 768px) {
          .auth-layout {
            grid-template-columns: 1fr;
          }
          .auth-brand {
            display: none;
          }
          .auth-form-panel {
            min-height: 100vh;
            padding: 32px 24px;
          }
          .auth-card {
            max-width: 400px;
          }
          .auth-mobile-logo {
            display: block;
          }
        }

        /* ── SMALL MOBILE ── */
        @media (max-width: 400px) {
          .auth-form-panel {
            padding: 24px 20px;
          }
        }

        /* ── REDUCED MOTION ── */
        @media (prefers-reduced-motion: reduce) {
          .auth-task-word {
            transition-duration: 0.01ms !important;
          }
        }
      `}</style>

      <div className="auth-layout">
        {/* LEFT: BRAND PANEL */}
        <div className="auth-brand" aria-hidden="true" inert>
          <div className="auth-brand-top">
            <Logo tone="cream" size="lg" withMark />
          </div>

          <div className="auth-brand-middle">
            <p className="auth-brand-welcome">Welcome back</p>
            <h1 className="auth-brand-tagline">
              I finished
              <br />
              <em className="auth-task-rotate" aria-live="polite">
                {TASKS.map((task, i) => (
                  <span
                    key={task}
                    className={`auth-task-word${
                      i === currentIndex ? " auth-task-word--active" : ""
                    }${i === exitIndex ? " auth-task-word--exit" : ""}`}
                  >
                    {task}
                  </span>
                ))}
              </em>
              <br />
              while you were gone.
            </h1>
          </div>

          <div className="auth-brand-bottom" />
        </div>

        {/* RIGHT: SIGN-IN FORM */}
        <main className="auth-form-panel">
          <div className="auth-card">
            <div className="auth-header">
              {/* Mobile-only logo */}
              <div className="auth-mobile-logo">
                <Logo size="md" withMark />
              </div>
              <h2 className="auth-title">Welcome back</h2>
              <p className="auth-subtitle">
                Sign in to your Lightbulb account to continue.
              </p>
            </div>

            <SignIn fallbackRedirectUrl="/voice" signUpFallbackRedirectUrl="/voice" />

            <p className="auth-footer">
              Don&apos;t have an account?{" "}
              <Link href="/sign-up">Sign up</Link>
            </p>
          </div>
        </main>
      </div>
    </>
  );
}
