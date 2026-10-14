import Image from "next/image";

import { HEADLINES, type HeadlineVariant } from "@/lib/headlineVariant";

import { HeroCtas } from "./HeroCtas";

/**
 * Landing hero — words left, artwork right.
 *
 * Replaces the previous `HeroFunnel`. NOTE: that component captured a brief to
 * sessionStorage (LANDING_BRIEF_STORAGE_KEY) which `useLandingBriefReplay`
 * replayed after workspace activation. This hero does not capture a brief, so
 * that replay path now has no producer on the landing page. Removing the funnel
 * was an explicit owner decision on 2026-09-11.
 *
 * The painting sits flush under the navbar and bleeds off the right edge; the
 * product cards are inset INSIDE it and never break the frame.
 */
export function Hero({ variant = "A" }: { variant?: HeadlineVariant }) {
  return (
    <header className="lb-hero">
      <div className="lb-hero-copy">
        <div>
          <h1>
            {variant === "A" ? (
              <>
                Your team and your AI employees,{" "}
                <br className="lb-brk" />
                in one place.
              </>
            ) : (
              HEADLINES.B
            )}
          </h1>
          <p className="lb-sub">
            A CEO, Engineering, Finance and Growth agent in your team&rsquo;s channels. Mention one, it does the
            work, and the result comes back where everyone can see it, approve it, or pick it up.
          </p>
          <HeroCtas variant={variant} />
        </div>
      </div>

      <div className="lb-art">
        <Image
          src="/landing/string-lights.jpg"
          alt="Strings of warm bulbs hung against a night sky"
          fill
          priority
          sizes="50vw"
          style={{ objectFit: "cover" }}
        />

        <div className="lb-float lb-f-run lb-ui lb-float-2">
          <div className="lb-mh">
            <b>Growth</b>
            <time>9:04</time>
            <span className="lb-tag">needs you</span>
          </div>
          <div className="lb-ask">
            <p>Launch post is drafted. Want to read it before it goes out?</p>
            {/* Decorative: this is a screenshot, not a control. */}
            <div className="lb-ask-r" aria-hidden="true">
              <span className="y">Approve</span>
              <span>Edit</span>
            </div>
          </div>
        </div>

        <div className="lb-float lb-f-app lb-ui">
          <div className="lb-wbar">
            <i />
            <i />
            <i />
          </div>
          <div className="lb-cols">
            <div className="lb-sb">
              <p>Channels</p>
              <div className="on">
                <span className="lb-hash">#</span>launch
              </div>
              <div>
                <span className="lb-hash">#</span>support
              </div>
              <div>
                <span className="lb-hash">#</span>finance
              </div>
              <p>Agents</p>
              <div>
                <span className="lb-lamp on" />
                CEO
              </div>
              <div>
                <span className="lb-lamp dim" />
                Growth
              </div>
              <div>
                <span className="lb-lamp out" />
                Finance
              </div>
            </div>
            <div className="lb-pane">
              <div className="lb-ct">
                <span className="lb-hash">#</span>
                <b>launch</b>
                <span className="lb-faces" style={{ marginLeft: "auto" }}>
                  <span className="lb-face">DK</span>
                  <span className="lb-face">MR</span>
                  <span className="lb-face ag">C</span>
                </span>
              </div>
              <div className="lb-m">
                <div className="lb-av hu">DK</div>
                <div>
                  <div className="lb-mh">
                    <b>Aden</b>
                    <time>9:02</time>
                  </div>
                  <p className="lb-tx">
                    Signups doubled overnight. <span className="lb-at">@CEO</span> what changed?
                  </p>
                </div>
              </div>
              <div className="lb-m">
                <div className="lb-av ag">C</div>
                <div>
                  <div className="lb-mh">
                    <b>CEO</b>
                    <time>9:02</time>
                  </div>
                  <p className="lb-tx">Traffic is all from one post. Pulling the numbers now.</p>
                  <div className="lb-reacts">
                    <span className="lb-re">👍 2</span>
                    <span className="lb-re">3 replies</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </header>
  );
}

export default Hero;
