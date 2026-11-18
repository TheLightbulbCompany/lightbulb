import Image from "next/image";

/**
 * Setup is five steps in the app (voice/Onboarding.tsx); these three cards are
 * what they come down to. Each card is artwork with a small copy of that
 * step's real controls laid over it.
 */
export function HowItWorks() {
  return (
    <section className="lb-steps" id="how">
      <div className="lb-wrap">
        <h2 className="lb-h2">Set up in five steps. Here is what they come down to.</h2>

        <div className="lb-cards3">
          <article className="lb-card3">
            <div className="lb-card-art" aria-hidden="true">
              <Image src="/landing/step-channel.jpg" alt="" fill sizes="(max-width: 980px) 100vw, 33vw" style={{ objectFit: "cover", objectPosition: "center 42%" }} />
              <div className="lb-compose">
                <div className="lb-compose-h">
                  <b>Your website</b>
                </div>
                <div className="lb-compose-b">
                  <div className="lb-compose-in">yoursalon.com</div>
                  <div className="lb-compose-f">
                    <span className="lb-send">Read my site</span>
                  </div>
                </div>
              </div>
            </div>
            <div className="lb-card-b">
              <h3>Tell it about your business</h3>
              <p>
                Paste your website and it drafts your hours, services and prices. You check the draft and fix
                anything it guessed.
              </p>
            </div>
          </article>

          <article className="lb-card3">
            <div className="lb-card-art" aria-hidden="true">
              <Image src="/landing/step-agents.jpg" alt="" fill sizes="(max-width: 980px) 100vw, 33vw" style={{ objectFit: "cover" }} />
              <div className="lb-rows">
                <div className="lb-row3">
                  <span className="lb-av ag">N</span>
                  <span className="lb-who3">
                    <b>Nova</b>
                    <span>Voice: Marin</span>
                  </span>
                </div>
                <div className="lb-row3">
                  <span className="lb-who3">
                    <b>Books appointments</b>
                  </span>
                  <span className="lb-chip3 yes">On</span>
                </div>
                <div className="lb-row3">
                  <span className="lb-who3">
                    <b>Also speaks Spanish</b>
                  </span>
                  <span className="lb-chip3">Off</span>
                </div>
              </div>
            </div>
            <div className="lb-card-b">
              <h3>Make it yours</h3>
              <p>
                Name it Nova, or anything you like. Pick one of three voices, write its greeting, and choose what it
                may do.
              </p>
            </div>
          </article>

          <article className="lb-card3">
            <div className="lb-card-art" aria-hidden="true">
              <Image src="/landing/step-work.jpg" alt="" fill sizes="(max-width: 980px) 100vw, 33vw" style={{ objectFit: "cover", objectPosition: "center 38%" }} />
              <div className="lb-rows">
                <div className="lb-row3">
                  <span className="lb-who3">
                    <b>Keep my number</b>
                    <span>Missed and busy calls only</span>
                  </span>
                  <span className="lb-chip3 yes">Chosen</span>
                </div>
                <div className="lb-row3">
                  <span className="lb-who3">
                    <b>Get a new number</b>
                    <span>Choose your area code</span>
                  </span>
                </div>
              </div>
            </div>
            <div className="lb-card-b">
              <h3>Keep your number</h3>
              <p>
                Forward missed and busy calls, or all of them. The app gives you your carrier&rsquo;s code and checks
                that forwarding works. Or get a new local number.
              </p>
            </div>
          </article>
        </div>
        <p className="lb-sec-sub">Your line goes live when you choose the plan.</p>
      </div>
    </section>
  );
}

export default HowItWorks;
