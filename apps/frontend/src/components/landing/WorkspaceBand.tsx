import Image from "next/image";
import Link from "next/link";

import { Arrow } from "./Arrow";

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
        <p className="lb-eyebrow">A complete workspace</p>
        <h2 className="lb-h2">
          Everything you need
          <br className="lb-brk" />
          to run the company.
        </h2>
        <div className="lb-platform-cta">
          <Link href="/sign-up" className="lb-btn lb-btn-p lb-btn-xl">
            Get started free
            <Arrow />
          </Link>
        </div>
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
                Friday
              </div>
              <div className="lb-mi">
                <span className="lb-lamp dim" />
                Atlas
              </div>
              <div className="lb-mi">
                <span className="lb-lamp out" />
                Vera
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
                  <span className="lb-face ag">F</span>
                </span>
              </div>
              <div className="lb-mfeed">
                <Msg who="Mara" at="8:41" avatar="MR" kind="hu">Morning — pricing page is live on prod.</Msg>
                <Msg who="Friday" at="8:44" avatar="F" kind="ag">Confirmed, it&apos;s serving. Smoke test passed on all three plans.</Msg>
                <Msg who="Aden" at="9:02" avatar="DK" kind="hu">
                  FAQ still says 14-day trial and we killed that in July.{" "}
                  <span className="lb-at">@Friday</span> fix it?
                </Msg>
                <div className="lb-m">
                  <div className="lb-av ag">F</div>
                  <div>
                    <div className="lb-mh">
                      <b>Friday</b>
                      <time>9:02</time>
                      <span className="lb-tag">working</span>
                    </div>
                    <p className="lb-tx">Three places say it. Rewriting and re-running the snapshot test.</p>
                    <div className="lb-run">
                      <div className="lb-run-h">
                        <span className="lb-lamp on" />
                        edit · 3 files
                      </div>
                      <div className="lb-run-b">
                        <s>PricingThreeCard.tsx</s> · <s>faq.mdx</s> · <s>plans.ts</s>
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
                  <div className="lb-av ag">F</div>
                  <div>
                    <div className="lb-mh">
                      <b>Friday</b>
                      <time>9:06</time>
                      <span className="lb-tag">needs you</span>
                    </div>
                    <p className="lb-tx">That one&apos;s a legal claim — I&apos;d rather you approve the wording than have me guess.</p>
                  </div>
                </div>
              </div>
              <div className="lb-mcomp">Message #launch — type @ to reach an agent</div>
            </div>

            <aside className="lb-mside">
              <p className="lb-mh2" style={{ paddingTop: "0.1rem" }}>Needs you</p>
              <div className="lb-mcard">
                <b>Approve refund wording</b>
                <p>Friday · #launch · 4m ago</p>
              </div>
              <p className="lb-mh2">Running</p>
              <div className="lb-mcard">
                <b>Fix trial copy</b>
                <p>Friday · 3 files edited</p>
                <div className="lb-mbar">
                  <i />
                </div>
              </div>
              <p className="lb-mh2">This month</p>
              <div className="lb-mcard">
                <b>128 runs</b>
                <p>41 needed a person</p>
              </div>
            </aside>
          </div>
        </div>
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
