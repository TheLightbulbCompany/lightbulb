import { CHAPTERS, PRODUCTS } from "@/content";
import { LampMark } from "./Logo";

/**
 * Nav, the chapter rail, the scrub bar, and the one leader label. The rail dots,
 * scrub dot, label and leader <svg> carry ids the scene's driver writes to each
 * frame (scene/Scene.tsx `chromeEls`); the page renders fine if it never does.
 *
 * The nav lists only what exists; the idea-stage chapters share one
 * "What's next" link so the page does not sell them as products.
 */
export function Chrome() {
  const live = PRODUCTS.filter((p) => !("idea" in p));
  const firstIdea = PRODUCTS.find((p) => "idea" in p);
  return (
    <>
      <nav className="nav">
        <a className="brand" href="#hero">
          <LampMark />
          <span>The Lightbulb Company</span>
        </a>
        <div className="links">
          {live.map((p) => (
            <a key={p.id} href={`#${p.id}`}>
              {p.name}
            </a>
          ))}
          {firstIdea ? <a href={`#${firstIdea.id}`}>What&rsquo;s next</a> : null}
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
