import Link from "next/link";
import { Fragment } from "react";

import { HOME_FAQ } from "@/lib/homeFaq";

function withPrivacyLink(text: string) {
  const parts = text.split("Privacy Policy");
  return parts.map((part, i) => (
    <Fragment key={i}>
      {part}
      {i < parts.length - 1 && <Link href="/privacy">Privacy Policy</Link>}
    </Fragment>
  ));
}

// The comparison page that answers an objection at length (11 task 7).
const MORE: Record<string, { href: string; label: string }> = {
  "We already have Slack.": { href: "/compare/slack", label: "Lightbulb vs Slack AI" },
};

/** The four objections, answered. Also emitted as FAQPage JSON-LD on `/`. */
export function HomeFaq() {
  return (
    <section className="lb-faq" id="faq">
      <div className="lb-wrap">
        <p className="lb-eyebrow">Questions</p>
        <h2 className="lb-h2">What teams ask first.</h2>
        <dl className="lb-faq-list">
          {HOME_FAQ.map(({ q, a }) => (
            <div key={q} className="lb-faq-item">
              <dt>{q}</dt>
              <dd>
                {withPrivacyLink(a)}
                {MORE[q] && (
                  <>
                    {" "}
                    <Link href={MORE[q].href}>{MORE[q].label} →</Link>
                  </>
                )}
              </dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}
