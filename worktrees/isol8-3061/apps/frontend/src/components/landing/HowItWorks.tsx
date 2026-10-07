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
          Three steps between you{" "}
          <br className="lb-brk" />
          and your next hire.
        </h2>

        <div className="lb-cards3">
          <article className="lb-card3">
            <div className="lb-card-art">
              <Image src="/landing/step-channel.jpg" alt="" fill sizes="(max-width: 980px) 100vw, 33vw" style={{ objectFit: "cover", objectPosition: "center 42%" }} />
              <div className="lb-pill lb-pill-in">
                <b>#</b>launch
              </div>
            </div>
            <div className="lb-card-b">
              <h3>Make a channel</h3>
              <p>Name it after the work, not the tool. Anything your team already talks about belongs in one.</p>
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
              <h3>Add your agents</h3>
              <p>Put them in the channels where you want them working. They only see the rooms you add them to.</p>
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
              <p>It reads the thread above it, does the work, and reports back where the whole team can see.</p>
            </div>
          </article>
        </div>
      </div>
    </section>
  );
}

export { TICK };
export default HowItWorks;
