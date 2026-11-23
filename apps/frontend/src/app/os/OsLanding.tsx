import Link from "next/link";
import { Suspense } from "react";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";

import { DownloadBlock, OsInvite, SOON } from "./OsInvite";
import { SparkGet } from "./SparkDownload";

export type SparkPrice = { priceText: string; trialDays: number } | null;
import { SparkStage } from "./SparkStage";


const SYSTEM = "For Mac with Apple silicon (M1 or later). Requires macOS 14 or later.";
const FRIEND = "Each friend who joins with your link adds a free day.";
// The first conversation (Spark 2.1.9 and later). Plain constants: the owner approves this wording.
const FIRST_TITLE = "Starts small";
const FIRST_LINE = "Its first hello: it circles what it sees, then does one small thing you ask.";
const HI_STEP = "Say hi. Spark shows what it sees and does one small thing.";

const Key = ({ k }: { k: string }) => <span className="os-key">{k}</span>;

// Spark's landing page. Every price and trial length comes from GET /os/config (`price`); without it
// the sentences that carry numbers are omitted or said without them.
export function OsLanding({ ready, price }: { ready: boolean; price: SparkPrice }) {
  const faq: [string, React.ReactNode][] = [
    [
      "Which Macs does Spark work on?",
      "A Mac with Apple silicon (M1 or later) running macOS 14 or later. There is no version for Intel Macs.",
    ],
    [
      "Do I need ChatGPT or Claude?",
      "Yes. Spark runs on your own ChatGPT account or a Claude plan of Pro or higher. Claude Free cannot run it. You connect one during setup.",
    ],
    [
      "What does it cost?",
      price
        ? `Free for ${price.trialDays} days, then ${price.priceText}. It renews monthly until you cancel. ${FRIEND}`
        : "See the price when you download.",
    ],
    [
      "What can Spark see?",
      "Your screen, so it can click, type and move files for you. You allow three Mac permissions during setup. What Spark sees and does goes to your own ChatGPT or Claude account, not to us, and your files and history stay on your Mac.",
    ],
    [
      "How do I cancel?",
      "In Spark, open Settings › Account › Manage subscription. Your plan renews monthly until you cancel.",
    ],
    [
      "Is my voice stored?",
      "No. Your voice is processed by OpenAI to understand and answer you, and we do not store the audio.",
    ],
  ];

  return (
    <main className="ld-page min-h-screen">
      <Navbar />
      <section className="os-hero">
        <div className="lb-wrap">
          <SparkStage
            level="hero"
            className="os-stage"
            label="Spark: a small blue creature with two eyes inside a glowing pixel-art light bulb"
          />
          <div className="os-copy">
            <h1>Meet Spark</h1>
            <p className="os-line">One-click voice interface for your Mac.</p>
            <p className="os-how-line">
              Hold <Key k="fn" /> and say what you want done. Spark does it on your Mac.
            </p>
            <div className="os-cta os-act" id="get">
              <Suspense fallback={<DownloadBlock ready={ready} source="os_hero" />}>
                <OsInvite ready={ready} source="os_hero" />
              </Suspense>
              {price && (
                <p className="os-small">
                  Free for {price.trialDays} days, then {price.priceText}. Renews monthly until you cancel.
                </p>
              )}
              <p className="os-small os-legal">
                <Link href="/terms">Terms</Link>
                <Link href="/privacy">Privacy</Link>
              </p>
              <p className="os-small">{SYSTEM}</p>
            </div>
          </div>
        </div>
      </section>

      <section className="os-sec" aria-labelledby="os-how">
        <div className="lb-wrap">
          <h2 className="os-h2" id="os-how">
            How Spark works
          </h2>
          <ol className="os-steps">
            <li>
              <span className="n">1</span>
              <h3>
                Hold <Key k="fn" />
              </h3>
              <p>Hold it any time to talk to Spark.</p>
            </li>
            <li>
              <span className="n">2</span>
              <h3>Say it</h3>
              <p>Talk the way you would to a person sitting next to you.</p>
              <q className="os-say">Put all the PDFs on my Desktop in a folder called Receipts.</q>
            </li>
            <li>
              <span className="n">3</span>
              <h3>Spark does it</h3>
              <p>It looks at your screen, then clicks, types and moves files for you, on your Mac.</p>
            </li>
          </ol>
        </div>
      </section>

      <section className="os-sec" aria-labelledby="os-can">
        <div className="lb-wrap">
          <h2 className="os-h2" id="os-can">
            What Spark can do
          </h2>
          <div className="os-can">
            <div>
              <h3>See your screen</h3>
              <p>It works with what is in front of you, so you can say &quot;this&quot;.</p>
            </div>
            <div>
              <h3>Circle anything</h3>
              <p>
                Press <Key k="fn" /> + <Key k="C" />, circle something on screen, and say what to do with it.
              </p>
            </div>
            <div>
              <h3>{FIRST_TITLE}</h3>
              <p>{FIRST_LINE}</p>
            </div>
            <div>
              <h3>Keep listening</h3>
              <p>
                Double-press <Key k="fn" /> and Spark keeps listening while you talk.
              </p>
            </div>
          </div>
        </div>
      </section>

      <section className="os-sec" aria-labelledby="os-brain">
        <div className="lb-wrap os-brain">
          <div>
            <h2 className="os-h2" id="os-brain">
              Runs on your ChatGPT or Claude
            </h2>
            <p>Spark uses your own ChatGPT or Claude account as its brain. You connect it once, in the first minute.</p>
          </div>
          <ol className="os-minis" aria-label="Setup takes about a minute">
            {(
              [
                [0.3, "Account", "Sign in."],
                [0.62, "Brain", "Connect ChatGPT or Claude."],
                [0.8, "Mac", "Allow three Mac permissions."],
                [1, "Hi", HI_STEP],
              ] as const
            ).map(([lit, step, text]) => (
              <li key={step}>
                <SparkStage level={lit} className="os-mini" />
                <b>{step}</b>
                <span>{text}</span>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className="os-sec" aria-labelledby="os-priv">
        <div className="lb-wrap os-priv">
          <h2 className="os-h2" id="os-priv">
            Where your things go
          </h2>
          <ul>
            <li>Your voice goes to OpenAI so Spark can understand and answer you. We don&apos;t store the audio.</li>
            <li>What Spark sees and does on your Mac goes to your own ChatGPT or Claude account, not to us.</li>
            <li>Your files and history stay on your Mac.</li>
          </ul>
        </div>
      </section>

      <section className="os-sec" aria-labelledby="os-faq">
        <div className="lb-wrap os-faq">
          <h2 className="os-h2" id="os-faq">
            Questions
          </h2>
          <div>
            {faq.map(([q, a]) => (
              <details key={q}>
                <summary>{q}</summary>
                <p>{a}</p>
              </details>
            ))}
          </div>
        </div>
      </section>

      <section className="os-end" aria-labelledby="os-end">
        <div className="lb-wrap">
          <SparkStage level={1} className="os-mini" />
          <div>
            <h2 id="os-end">{price ? `Try Spark free for ${price.trialDays} days.` : "Try Spark."}</h2>
            <p className="os-price">
              {price && `Then ${price.priceText}. `}
              {FRIEND}
            </p>
            <div className="os-cta">
              {ready ? <SparkGet source="os_closing" /> : <p className="os-soon">{SOON}</p>}
              <p className="os-small">
                {price && "Renews monthly until you cancel. "}For Mac with Apple silicon (M1 or later), macOS 14 or
                later.
              </p>
            </div>
          </div>
        </div>
      </section>
      <Footer cta={false} />
    </main>
  );
}
