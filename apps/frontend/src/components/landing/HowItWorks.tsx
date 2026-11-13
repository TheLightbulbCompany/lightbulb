import Image from "next/image";

const TICK = (
  <svg width="13" height="13" viewBox="0 0 14 14" aria-hidden="true">
    <path d="M2.5 7.3l3 3 6-6.2" fill="none" stroke="#B68235" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

/** Three steps, each a card with artwork on top and product UI laid over it. */
export function HowItWorks() {
  return (
    <section className="lb-steps" id="how">
      <div className="lb-wrap">
        <p className="lb-eyebrow">How it works</p>
        <h2 className="lb-h2">
          Bring the team first.{" "}
          <br className="lb-brk" />
          Then the agents.
        </h2>

        <div className="lb-cards3">
          <article className="lb-card3">
            <div className="lb-card-art">
              <Image src="/landing/step-channel.jpg" alt="" fill sizes="(max-width: 980px) 100vw, 33vw" style={{ objectFit: "cover", objectPosition: "center 42%" }} />
              <div className="lb-rows">
                <div className="lb-row3">
                  <span className="lb-who3">
                    <b>Invite to Lightbulb</b>
                  </span>
                </div>
                <div className="lb-row3">
                  <span className="lb-av hu">MR</span>
                  <span className="lb-who3">
                    <b>Mara Reyes</b>
                  </span>
                </div>
                <div className="lb-row3">
                  <span className="lb-av hu">JP</span>
                  <span className="lb-who3">
                    <b>Jonah Pike</b>
                  </span>
                  <span className="lb-chip3 yes">Send invite</span>
                </div>
              </div>
            </div>
            <div className="lb-card-b">
              <h3>Invite your team</h3>
              <p>Send your cofounder the join link. They land in #general with everyone, and there are no per-seat fees.</p>
            </div>
          </article>

          <article className="lb-card3">
            <div className="lb-card-art">
              <Image src="/landing/step-agents.jpg" alt="" fill sizes="(max-width: 980px) 100vw, 33vw" style={{ objectFit: "cover" }} />
              <div className="lb-rows">
                <div className="lb-row3">
                  <span className="lb-av ag">C</span>
                  <span className="lb-who3">
                    <b>CEO</b>
                    <span>Runs the company</span>
                  </span>
                  <span className="lb-chip3 yes">In channel</span>
                </div>
                <div className="lb-row3">
                  <span className="lb-av ag">A</span>
                  <span className="lb-who3">
                    <b>Analytics</b>
                    <span>Reads the numbers</span>
                  </span>
                  <span className="lb-chip3">Add</span>
                </div>
              </div>
            </div>
            <div className="lb-card-b">
              <h3>Add agents where the work is</h3>
              <p>Put the CEO in #general and Engineering in #launch. An agent only sees the rooms you add it to.</p>
            </div>
          </article>

          <article className="lb-card3">
            <div className="lb-card-art">
              <Image src="/landing/step-work.jpg" alt="" fill sizes="(max-width: 980px) 100vw, 33vw" style={{ objectFit: "cover", objectPosition: "center 38%" }} />
              <div className="lb-compose">
                <div className="lb-compose-h">
                  <span className="lb-hash">#</span>
                  <b>launch</b>
                </div>
                <div className="lb-compose-b">
                  <div className="lb-compose-in">
                    <span className="lb-at">@Analytics</span> where are the new signups coming from?
                  </div>
                  <div className="lb-compose-f">
                    <i />
                    <i />
                    <i />
                    <span className="lb-send">Send</span>
                  </div>
                </div>
              </div>
            </div>
            <div className="lb-card-b">
              <h3>Mention one and go</h3>
              <p>
                It reads the thread above it, does the work, and posts the result in the room. Anyone can approve it,
                redirect it, or pick it up.
              </p>
            </div>
          </article>
        </div>
      </div>
    </section>
  );
}

export { TICK };
export default HowItWorks;
