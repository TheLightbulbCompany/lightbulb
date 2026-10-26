import {
  Bell,
  Bold,
  ChevronDown,
  ChevronRight,
  Code,
  Images,
  Italic,
  Link2,
  List,
  ListChecks,
  ListOrdered,
  MessageSquare,
  Plus,
  Search,
  Send,
  Strikethrough,
} from "lucide-react";

import { HEADLINES, headlineCookieScript, type HeadlineVariant } from "@/lib/headlineVariant";

import { HeroCtas } from "./HeroCtas";

/**
 * Landing hero — words left, the product right.
 *
 * Replaces the previous `HeroFunnel`. NOTE: that component captured a brief to
 * sessionStorage (LANDING_BRIEF_STORAGE_KEY) which `useLandingBriefReplay`
 * replayed after workspace activation. This hero does not capture a brief, so
 * that replay path now has no producer on the landing page. Removing the funnel
 * was an explicit owner decision on 2026-09-11.
 *
 * The right half is an HTML copy of the real /chat workspace (rail, sidebar,
 * #general, composer) on a light ground. It replaced a night-sky painting after
 * the hero teardown found the dark art pulled the eye off the copy and buttons.
 * Keep it in step with the app's chrome when that changes.
 */
export function Hero({ variant = "A" }: { variant?: HeadlineVariant }) {
  return (
    <header className="lb-hero">
      <script dangerouslySetInnerHTML={{ __html: headlineCookieScript(variant) }} />
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
              HEADLINES.C
            )}
          </h1>
          <p className="lb-sub">
            Six AI employees in your team&rsquo;s channels. Mention one and the work comes back for everyone to
            see.
          </p>
          <HeroCtas variant={variant} />
        </div>
      </div>

      {/* Decorative: a faithful copy of the real /chat workspace, not a control. */}
      <div className="lb-art" aria-hidden="true">
        <div className="lb-app lb-ui">
          <div className="lb-wbar">
            <i />
            <i />
            <i />
          </div>
          <div className="lb-app-cols">
            <nav className="lb-app-rail">
              <span>
                <Search />
              </span>
              <span className="on">
                <MessageSquare />
                <small>Chat</small>
              </span>
              <span>
                <Bell />
                <em>2</em>
                <small>Activity</small>
              </span>
              <span>
                <ListChecks />
                <small>Work</small>
              </span>
              <span>
                <Images />
                <small>Library</small>
              </span>
            </nav>

            <aside className="lb-app-sb">
              <div className="lb-app-org">
                Acme <ChevronDown />
              </div>
              <div className="lb-app-sbh">
                Conversations <Plus />
              </div>
              <p>
                <ChevronRight /> Team
              </p>
              <p>
                <ChevronDown /> Channels
              </p>
              <div className="lb-app-ch on">
                <span className="lb-hash">#</span>general
              </div>
              <div className="lb-app-ch">
                <span className="lb-hash">#</span>launch
              </div>
              <p>
                <ChevronDown /> Sessions
              </p>
              <div className="lb-app-note">No one is sharing a session.</div>
              <div className="lb-app-ch">
                <Plus /> Invite someone
              </div>
            </aside>

            <div className="lb-app-main">
              <div className="lb-app-top">
                <b>
                  <span className="lb-hash">#</span> general
                </b>
                <span className="lb-app-search">
                  <Search /> Search
                </span>
                <span className="lb-faces">
                  <span className="lb-face ag">GR</span>
                  <span className="lb-face ag">CE</span>
                  <span className="lb-face ag">FI</span>
                  <span className="lb-face">+3</span>
                </span>
              </div>

              <div className="lb-app-feed">
                <div className="lb-app-intro">
                  <b>This is the beginning of #general.</b>
                  <span>
                    Growth, CEO, Finance, Customer Operations, Analytics, and Engineering are here. Mention one when
                    you want help.
                  </span>
                </div>

                <div className="lb-m">
                  <div className="lb-av hu">AK</div>
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
                  <div className="lb-av ag">CE</div>
                  <div>
                    <div className="lb-mh">
                      <b>CEO</b>
                      <time>9:02</time>
                    </div>
                    <p className="lb-tx">
                      Traffic is all from one post. <span className="lb-at">@Growth</span> can you draft a follow-up?
                    </p>
                  </div>
                </div>
                <div className="lb-m">
                  <div className="lb-av ag">GR</div>
                  <div>
                    <div className="lb-mh">
                      <b>Growth</b>
                      <time>9:04</time>
                      <span className="lb-tag">needs you</span>
                    </div>
                    <div className="lb-ask">
                      <p>Launch post is drafted. Want to read it before it goes out?</p>
                      <div className="lb-ask-r">
                        <span className="y">Approve</span>
                        <span>Edit</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <div className="lb-app-compose">
                <div className="lb-app-tools">
                  <Bold />
                  <Italic />
                  <Strikethrough />
                  <List />
                  <ListOrdered />
                  <Code />
                  <Link2 />
                </div>
                <div className="lb-app-input">
                  Message the conversation · @ to mention
                  <Send />
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
