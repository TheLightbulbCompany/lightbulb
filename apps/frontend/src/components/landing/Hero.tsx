import { HeroCtas } from "./HeroCtas";

/**
 * Landing hero: words left, the owner's Home right.
 *
 * The right half is an HTML copy of the real Home (`/voice`, VoiceHome.tsx):
 * the call timeline with outcome tags, and one call open with "What {name} did"
 * and the transcript. Names and calls are made up; "Nova" is an example name.
 * Keep the labels in step with VoiceHome when they change.
 */
export function Hero() {
  return (
    <header className="lb-hero">
      <div className="lb-hero-copy">
        <div>
          <h1>Your phone, answered and booked while your hands are busy.</h1>
          <p className="lb-sub">
            Lightbulb gives your salon, spa or barbershop an AI receptionist with the name you choose. It answers
            day and night, books appointments, takes messages, and brings you anything it can&rsquo;t decide.
          </p>
          <HeroCtas />
        </div>
      </div>

      {/* Decorative: a copy of the real Home screen, not a control. */}
      <div className="lb-art" aria-hidden="true">
        <div className="lb-app lb-ui">
          <div className="lb-wbar">
            <i />
            <i />
            <i />
          </div>
          <div className="lb-home">
            <div className="lb-home-list">
              <div className="lb-app-top">
                <b>Home</b>
              </div>
              <ul className="lb-calls">
                <li className="on">
                  <time>7:12 pm</time>
                  <span className="lb-tag">Booked</span>
                  <span>Dana booked a haircut with Sam, Saturday at 10:00.</span>
                </li>
                <li>
                  <time>6:40 pm</time>
                  <span className="lb-tag warn">Needs you</span>
                  <span>Asked about a bridal party. Took a message.</span>
                </li>
                <li>
                  <time>2:15 pm</time>
                  <span className="lb-tag">Moved</span>
                  <span>Moved Priya&rsquo;s color to Thursday at 2:00.</span>
                </li>
                <li>
                  <time>11:03 am</time>
                  <span className="lb-tag">Answered</span>
                  <span>Asked if you&rsquo;re open Monday. Told them 10 to 6.</span>
                </li>
              </ul>
            </div>
            <div className="lb-home-pane">
              <b>Dana &middot; 7:12 pm</b>
              <h4>What Nova did</h4>
              <ol>
                <li>Checked Saturday morning openings</li>
                <li>Booked a haircut with Sam, Saturday 10:00</li>
              </ol>
              <h4>Transcript</h4>
              <p>
                <b>Nova</b>Hi, I&rsquo;m Nova, the salon&rsquo;s AI assistant. How can I help?
              </p>
              <p>
                <b>Dana</b>Do you have anything Saturday morning for a haircut?
              </p>
              <p>
                <b>Nova</b>Sam has 10:00 or 11:30. Which works for you?
              </p>
              <p>
                <b>Dana</b>10, please.
              </p>
            </div>
          </div>
        </div>
      </div>
    </header>
  );
}

export default Hero;
