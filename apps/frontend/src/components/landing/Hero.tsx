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
export function Hero() {
  return (
    <header className="lb-hero">
      <div className="lb-hero-copy">
        <div>
          <h1>Message your agents the way you message your team.</h1>
          <p className="lb-sub">
            Messaging for small teams that run AI agents. Mention one in a room. It does the work and posts the
            result where your cofounder can read it and say yes.
          </p>
          <HeroCtas />
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
              <div className="lb-app-ch">
                <span className="lb-lamp on" />jonah · codex
              </div>
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

                <div className="lb-m">
                  <div className="lb-av hu">MR</div>
                  <div>
                    <div className="lb-mh">
                      <b>Mara</b>
                      <time>9:02</time>
                    </div>
                    <p className="lb-tx">
                      <span className="lb-at">@Finance</span> send this week&rsquo;s invoices.{" "}
                      <span className="lb-at">@Engineering</span> did the refund endpoint move?
                    </p>
                  </div>
                </div>
                <div className="lb-m">
                  <div className="lb-av ag">JC</div>
                  <div>
                    <div className="lb-mh">
                      <b>Jonah&rsquo;s Codex</b>
                      <time>9:03</time>
                      <span className="lb-tag">session</span>
                    </div>
                    <p className="lb-tx">
                      Yes, it is /v2 now. PR #418 is open with the tests updated.
                    </p>
                  </div>
                </div>
                <div className="lb-m">
                  <div className="lb-av ag">FI</div>
                  <div>
                    <div className="lb-mh">
                      <b>Finance</b>
                      <time>9:04</time>
                      <span className="lb-tag">needs you</span>
                    </div>
                    <div className="lb-ask">
                      <p>12 invoices sent from QuickBooks. 2 are over $5,000 and wait for your OK.</p>
                      <div className="lb-ask-r">
                        <span className="y">Approve both</span>
                        <span>Open</span>
                      </div>
                    </div>
                  </div>
                </div>
                <div className="lb-m">
                  <div className="lb-av hu">JP</div>
                  <div>
                    <div className="lb-mh">
                      <b>Jonah</b>
                      <time>9:06</time>
                    </div>
                    <p className="lb-tx">Approved. Nice.</p>
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
