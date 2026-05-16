export function Features() {
  return (
    <section className="landing-features" id="features">
      <div className="section-inner">
        <p className="section-eyebrow reveal">Your Pod</p>
        <h2 className="section-h2 reveal">
          AI agents that fit into <em>your</em> tools.
        </h2>

        <div className="feat-grid">
          <div className="feat-card reveal reveal-d1">
            <div className="feat-icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2L2 7l10 5 10-5-10-5z" />
                <path d="M2 17l10 5 10-5" />
                <path d="M2 12l10 5 10-5" />
              </svg>
            </div>
            <h3 className="feat-title">Plugs Into Your Stack</h3>
            <p className="feat-desc">
              Gmail, Slack, Salesforce, Notion — your agents connect natively
              to the tools your team relies on. No new apps to learn, no
              workflows to rebuild.
            </p>
          </div>
          <div className="feat-card reveal reveal-d2">
            <div className="feat-icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
              </svg>
            </div>
            <h3 className="feat-title">Always In The Loop</h3>
            <p className="feat-desc">
              Message your agents on Slack, WhatsApp, or Discord — just like
              texting a teammate. Your container is always running, so your
              agents never stop working, even when you log off.
            </p>
          </div>
          <div className="feat-card reveal reveal-d3">
            <div className="feat-icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
              </svg>
            </div>
            <h3 className="feat-title">Acts Autonomously</h3>
            <p className="feat-desc">
              Your pod doesn&apos;t just answer — it acts. Schedule tasks, run
              workflows, send messages, and complete multi-step jobs in the
              background while you focus on what matters.
            </p>
          </div>
        </div>

        <div className="skills-strip">
          <div className="skills-strip-lede">
            <h3>
              Upgrade them <em>endlessly.</em>
            </h3>
            <p>
              Install new skills from a curated library. Your agents learn to
              use them automatically.
            </p>
            <a href="#" className="skills-strip-cta">
              Browse the skill store →
            </a>
          </div>
          <div className="skills-rail">
            <span className="skill-chip">
              <span className="skill-chip-dot" />
              <span>Memory</span>
              <span className="skill-chip-mono">installed</span>
            </span>
            <span className="skill-chip">
              <span className="skill-chip-dot" />
              <span>Developer</span>
              <span className="skill-chip-mono">installed</span>
            </span>
            <span className="skill-chip">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src="/logos/gmail.png" width={14} height={14} alt="" style={{ borderRadius: 3 }} />
              <span>Gmail</span>
            </span>
            <span className="skill-chip">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src="/logos/gcal.png" width={14} height={14} alt="" style={{ borderRadius: 3 }} />
              <span>Calendar</span>
            </span>
            <span className="skill-chip">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src="/logos/salesforce.png" width={14} height={14} alt="" />
              <span>Salesforce</span>
            </span>
            <span className="skill-chip">
              <span className="skill-chip-dot" style={{ background: "#888" }} />
              <span>Perplexity</span>
            </span>
            <span className="skill-chip">
              <span className="skill-chip-dot" style={{ background: "#888" }} />
              <span>Notion</span>
            </span>
            <span className="skill-chip" style={{ borderStyle: "dashed", color: "#999" }}>
              <span>+ 28 more</span>
            </span>
          </div>
        </div>
      </div>
    </section>
  );
}
