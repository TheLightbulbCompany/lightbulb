import { PRODUCTS } from "@/content";
import { Chrome } from "@/components/Chrome";
import { LightbulbScene } from "@/components/LightbulbScene";
import { SunriseMark } from "@/components/Logo";

const MAIL = "mailto:hello@thelightbulb.company";

export default function Page() {
  return (
    <>
      <LightbulbScene />
      <Chrome />
      <main>
        <section className="hero" id="hero">
          <p className="eyebrow">The Lightbulb Company</p>
          <h1>The business of ideas.</h1>
          <p className="lede">Every company starts in the dark. We build what comes after.</p>
        </section>

        <section className="strike" id="strike">
          <div>
            <p className="eyebrow">01 — The strike</p>
            <h2>
              Then someone <em>flicks it on.</em>
            </h2>
            <p className="lede" style={{ marginLeft: "auto" }}>
              One idea, lit. Everything we make is a room that light reaches.
            </p>
          </div>
        </section>

        {PRODUCTS.map((p) => (
          <section className="prod" id={p.id} key={p.id}>
            <div>
              <div className="idx">
                {p.index} <span className={`tag${p.tag === "Live" ? " live" : ""}`}>{p.tag}</span>
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
            <h2>Light is the only thing that gets bigger when you share it.</h2>
            <p className="lede">
              The Lightbulb Company is a holding company for products that make people more capable. Each one starts
              as a single idea, gets built by a small team, and stays lit long after launch.
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
            {PRODUCTS.map((p) => (
              <a key={p.id} href={"href" in p ? p.href : `#${p.id}`}>
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
