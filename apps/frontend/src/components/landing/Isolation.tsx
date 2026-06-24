/**
 * Isolation — the "containment" box.
 *
 * A single forest band that states the security posture in plain terms:
 * every pod is cloud-sandboxed and least-privilege, the differentiator
 * against locally-installed agents. Styled with the shared ld- tokens so
 * it reads as one deliberate accent within the cream editorial page.
 */
export function Isolation() {
  return (
    <section className="ld-iso-section" id="isolation">
      <div className="ld-section">
        <div className="ld-iso" data-ld-reveal>
          <div className="ld-iso-icon" aria-hidden="true">
            <svg
              width="28"
              height="28"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              <path d="M9 12l2 2 4-4" />
            </svg>
          </div>

          <div className="ld-iso-body">
            <span className="ld-iso-kicker">isolated by default</span>
            <h3>
              Your agent can&apos;t touch what you <em>don&apos;t hand it.</em>
            </h3>
            <p>
              Every pod runs in its own cloud sandbox, never on your laptop. It
              can&apos;t reach your whole machine the way a locally-installed
              agent can. It sees <strong>only the files and tools you
              connect</strong>, so even with your own API key, you set the
              scope, not the model.
            </p>
          </div>

          <span className="ld-iso-pill">sandboxed · scoped · yours</span>
        </div>
      </div>
    </section>
  );
}
