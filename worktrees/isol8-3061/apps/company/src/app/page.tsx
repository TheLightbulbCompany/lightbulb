import { PRODUCTS } from "@/content";
import { Chrome } from "@/components/Chrome";
import { LightbulbScene } from "@/components/LightbulbScene";
import { SunriseMark } from "@/components/Logo";

const MAIL = "mailto:hello@thelightbulb.company";

export default function Page() {
  const products = PRODUCTS.filter((p) => !("idea" in p));
  const ideas = PRODUCTS.filter((p) => "idea" in p);

  return (
    <>
      <LightbulbScene />
      <Chrome />
      <main>
        <section className="hero" id="hero">
          <p className="eyebrow">The Lightbulb Company</p>
          <h1>The business of ideas.</h1>
          <p className="lede">
            A small company that builds companies. One idea at a time, from the first spark to the lights staying on.
          </p>
        </section>

        <section className="strike" id="strike">
          <div>
            <p className="eyebrow">01 — The strike</p>
            <h2>
              Then someone <em>flicks it on.</em>
            </h2>
            <p className="lede" style={{ marginLeft: "auto" }}>
              Every product here began as a private thought that wouldn&rsquo;t go away. This is what happened after.
            </p>
          </div>
        </section>

        {PRODUCTS.map((p) => (
          <section className={`prod${"idea" in p ? " idea" : ""}`} id={p.id} key={p.id}>
            <div>
              <div className="idx">
                {p.index}
                <span className={`stage stage-${p.id}`}>{p.stage}</span>
                <span className="note">{p.note}</span>
              </div>
              <h2>{p.name}</h2>
              <p>
                {p.blurb}
                {"href" in p ? (
                  <>
                    {" "}
                    <a href={p.href}>{p.href.replace("https://", "")}</a>
                  </>
                ) : null}
              </p>
            </div>
          </section>
        ))}

        <section className="vision" id="vision">
          <div>
            <p className="eyebrow">One bulb, many rooms</p>
            <h2>Most ideas die in the dark.</h2>
            <p className="lede">
              Not for lack of light — for lack of someone to build them. The Lightbulb Company exists to be that
              someone: a small team that takes one idea at a time from spark to switched-on, and stays to keep it
              lit.
            </p>
          </div>
        </section>
      </main>

      <footer>
        <div>
          <div className="mark">
            <SunriseMark />
          </div>
          <a className="mail" href={MAIL}>
            hello@thelightbulb.company
          </a>
          <div className="meta">The business of ideas · Isol8 Inc., doing business as Lightbulb AI</div>
        </div>
        <div className="cols">
          <div>
            Products
            {products.map((p) => (
              <a key={p.id} href={"href" in p ? p.href : `#${p.id}`}>
                {p.name}
              </a>
            ))}
          </div>
          <div>
            Next
            {ideas.map((p) => (
              <a key={p.id} href={`#${p.id}`}>
                {p.name}
              </a>
            ))}
          </div>
          <div>
            Company
            <a href={MAIL}>Contact</a>
          </div>
        </div>
      </footer>
    </>
  );
}
