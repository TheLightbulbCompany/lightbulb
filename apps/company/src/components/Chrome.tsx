import { CHAPTERS, PRODUCTS } from "@/content";
import { LampMark } from "./Logo";

/**
 * Nav, the chapter rail, the scrub bar, and the one leader label. The rail dots,
 * scrub dot, label and leader <svg> carry ids the scene's driver writes to each
 * frame (scene/Scene.tsx `chromeEls`); the page renders fine if it never does.
 */
export function Chrome() {
  return (
    <>
      <nav className="nav">
        <a className="brand" href="#hero">
          <LampMark />
          <span>The Lightbulb Company</span>
        </a>
        <div className="links">
          {PRODUCTS.map((p) => (
            <a key={p.id} href={`#${p.id}`}>
              {p.name}
            </a>
          ))}
          <a href="mailto:hello@thelightbulb.company">Contact</a>
        </div>
      </nav>
      <div className="rail" id="rail">
        {CHAPTERS.map((c, i) => (
          <i key={c.id} className={i === 0 ? "on" : undefined} />
        ))}
      </div>
      <div className="scrub">
        <b id="scrubDot" />
      </div>
      <div id="labels">
        <svg id="leaders" />
        <div className="lbl" id="lbl">
          <span id="lblName" />
          <small id="lblSub" />
        </div>
      </div>
    </>
  );
}
