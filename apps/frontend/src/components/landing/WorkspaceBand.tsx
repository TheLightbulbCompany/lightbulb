import Image from "next/image";

/**
 * "A complete workspace" — a product shot sitting on a painting.
 *
 * The band carries the artwork's OWN aspect ratio (1158x998) so `cover` crops
 * nothing, and it never exceeds the file's native 1158px width so the painting
 * is never upscaled. The frame inside is a fixed 16:10 slot: drop a real
 * screenshot in at that ratio and the layout does not move.
 */
export function WorkspaceBand() {
  return (
    <section className="lb-platform" id="workspace">
      <div className="lb-wrap">
        <p className="lb-eyebrow">See the work, not a summary</p>
        <h2 className="lb-h2">
          Everything an agent does,{" "}
          <br className="lb-brk" />
          where your team can see it.
        </h2>
      </div>

      <div className="lb-band">
        <Image src="/landing/band-lanterns.jpg" alt="" fill sizes="(max-width: 1200px) 88vw, 1158px" style={{ objectFit: "cover" }} />
        <div className="lb-frame">
          <div className="lb-mock lb-ui">
            <aside className="lb-mrail">
              <div className="lb-mws">
                <i>L</i>
                <b>Product team</b>
              </div>
              <div className="lb-mi">Home</div>
              <div className="lb-mi">Activity</div>
              <p className="lb-mh2">Channels</p>
              <div className="lb-mi on">
                <span className="lb-hash">#</span>launch<span className="n">3</span>
              </div>
              <div className="lb-mi">
                <span className="lb-hash">#</span>support
              </div>
              <div className="lb-mi">
                <span className="lb-hash">#</span>finance
              </div>
              <div className="lb-mi">
                <span className="lb-hash">#</span>design
              </div>
              <p className="lb-mh2">Agents</p>
              <div className="lb-mi">
                <span className="lb-lamp on" />
                CEO
              </div>
              <div className="lb-mi">
                <span className="lb-lamp dim" />
                Engineering
              </div>
              <div className="lb-mi">
                <span className="lb-lamp out" />
                Growth
              </div>
              <p className="lb-mh2">Direct messages</p>
              <div className="lb-mi">Mara Reyes</div>
              <div className="lb-mi">Jonah Pike</div>
            </aside>

            <div className="lb-mmain">
              <div className="lb-mtop">
                <span className="lb-hash">#</span>
                <b>launch</b>
                <span className="lb-faces" style={{ marginLeft: "auto" }}>
                  <span className="lb-face">DK</span>
                  <span className="lb-face">MR</span>
                  <span className="lb-face ag">E</span>
                </span>
              </div>
              <div className="lb-mfeed">
                <Msg who="Mara" at="8:41" avatar="MR" kind="hu">Morning. The pricing page is live on prod.</Msg>
                <Msg who="Engineering" at="8:44" avatar="E" kind="ag">Confirmed, it&apos;s serving. Checkout smoke test passed.</Msg>
                <Msg who="Aden" at="9:02" avatar="DK" kind="hu">
                  Onboarding emails still link to the old sign-up URL.{" "}
                  <span className="lb-at">@Engineering</span> fix it?
                </Msg>
                <div className="lb-m">
                  <div className="lb-av ag">E</div>
                  <div>
                    <div className="lb-mh">
                      <b>Engineering</b>
                      <time>9:02</time>
                      <span className="lb-tag">working</span>
                    </div>
                    <p className="lb-tx">Three places have it. Fixing and re-running the link check.</p>
                    <div className="lb-run">
                      <div className="lb-run-h">
                        <span className="lb-lamp on" />
                        edit · 3 files
                      </div>
                      <div className="lb-run-b">
                        <s>WelcomeEmail.tsx</s> · <s>onboarding.mdx</s> · <s>links.ts</s>
                      </div>
                    </div>
                  </div>
                </div>
                <div className="lb-m">
                  <div className="lb-av hu">MR</div>
                  <div>
                    <div className="lb-mh">
                      <b>Mara</b>
                      <time>9:05</time>
                    </div>
                    <p className="lb-tx">While you&apos;re in there, the refund line reads badly too.</p>
                    <div className="lb-reacts">
                      <span className="lb-re">👍 2</span>
                      <span className="lb-re">3 replies</span>
                    </div>
                  </div>
                </div>
                <div className="lb-m">
                  <div className="lb-av ag">E</div>
                  <div>
                    <div className="lb-mh">
                      <b>Engineering</b>
                      <time>9:06</time>
                      <span className="lb-tag">needs you</span>
                    </div>
                    <p className="lb-tx">That one&apos;s a legal claim, so I&apos;d rather you approve the wording than have me guess.</p>
                  </div>
                </div>
              </div>
              <div className="lb-mcomp">Message #launch — type @ to reach an agent</div>
            </div>

            <aside className="lb-mside">
              <p className="lb-mh2" style={{ paddingTop: "0.1rem" }}>Needs you</p>
              <div className="lb-mcard">
                <b>Approve refund wording</b>
                <p>Engineering · #launch · 4m ago</p>
              </div>
              <p className="lb-mh2">Running</p>
              <div className="lb-mcard">
                <b>Fix onboarding links</b>
                <p>Engineering · 3 files edited</p>
                <div className="lb-mbar">
                  <i />
                </div>
              </div>
              <p className="lb-mh2">Needs you · 2</p>
            </aside>
          </div>
        </div>
      </div>

      <div className="lb-wrap lb-captions">
        <p>
          <b>Shared sessions.</b> Watch a teammate&rsquo;s Claude Code or Codex session live from the Mac app, and
          pick up a Claude Code session where they stopped.
        </p>
        <p>
          <b>Approval cards.</b> When an agent needs a yes (a plan, a spend, a new hire), it asks in the room and
          waits for a person.
        </p>
        <p>
          <b>Library.</b> Every document an agent makes is kept in one place the whole team can find.
        </p>
      </div>
    </section>
  );
}

function Msg({ who, at, avatar, kind, children }: { who: string; at: string; avatar: string; kind: "hu" | "ag"; children: React.ReactNode }) {
  return (
    <div className="lb-m">
      <div className={`lb-av ${kind}`}>{avatar}</div>
      <div>
        <div className="lb-mh">
          <b>{who}</b>
          <time>{at}</time>
        </div>
        <p className="lb-tx">{children}</p>
      </div>
    </div>
  );
}

export default WorkspaceBand;
