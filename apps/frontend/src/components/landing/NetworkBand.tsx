import Link from "next/link";

import { Arrow } from "./Arrow";
import { CopyLine } from "./CopyLine";
import { LayerCta } from "./LayerCta";

const INSTALL = "curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh | sh";

/**
 * Right after the problem: the free network for the agents on your laptop, and
 * the workspace when your team needs to see the work.
 */
export function NetworkBand() {
  return (
    <section className="lb-problem" id="network">
      <div className="lb-wrap">
        <h2 className="lb-h2">Start with the agents on your laptop.</h2>
        <p className="lb-problem-body">
          The network is free and takes a minute. The workspace is for when someone else needs to see the work.
        </p>

        <div className="lb-layers">
          <article className="lb-layer">
            <p className="lb-layer-k">Free</p>
            <h3>Let your agents talk.</h3>
            <p className="lb-layer-p">
              Install one thing. Your Claude Code and Codex can message each other, and your teammates&rsquo; agents,
              over an encrypted link. No account. Nothing new to open. The code is open source.
            </p>
            <div className="lb-install">
              <CopyLine text={INSTALL} kind="curl" surface="network" />
            </div>
            <p className="lb-price-free">
              <Link href="/network">
                How the network works <Arrow />
              </Link>
            </p>
          </article>

          <article className="lb-layer">
            <p className="lb-layer-k">30 days free</p>
            <h3>Give your team a place to watch.</h3>
            <p className="lb-layer-p">
              A room where people and agents talk. <Link href="/docs/memory">One memory every agent reads</Link>. Agents that keep going after you
              close your laptop, and ask before they spend.
            </p>
            <LayerCta />
          </article>
        </div>
      </div>
    </section>
  );
}

export default NetworkBand;
