"use client";

import { useState, useSyncExternalStore } from "react";

import { capture } from "@/lib/analytics";

import "./spark-phone.css";

// A Spark referral code: the referrer's Clerk user id without its `user_` prefix. Here, not in OsInvite,
// so the site menu can import this file without the invite's sign-up form.
export const CODE = /^[A-Za-z0-9]{1,64}$/;

// Hovering or focusing the button lights the page's bulb (decoration; no listener on the short page).
const light = (on: boolean) => window.dispatchEvent(new CustomEvent("spark:light", { detail: on }));

// Counts a click on any Download for Mac link. `source` names the placement: os_page (the short page),
// os_hero, os_closing, nav. Never the invite code itself: only whether the visit came from an invite.
// Every click gets a random `dl_id` (16 hex): it rides on the event and on the link as `?dl=` (the download
// request's URL), so the app's `app_first_launch` can claim it. The installer cannot carry it yet: see
// docs/spark-analytics-dl-id.md.
export function trackSparkDownload(source: string, link?: HTMLAnchorElement) {
  const ref = new URLSearchParams(window.location.search).get("ref") ?? "";
  const dl_id = Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, "0")).join("");
  if (link) link.search = `?dl=${dl_id}`;
  capture("spark_download_clicked", { source, dl_id, ...(CODE.test(ref) && { has_referral: true }) });
}

// A plain link (works with JavaScript off); the click is counted, never delayed.
export function SparkDownload({ source = "os_page" }: { source?: string }) {
  return (
    <a
      href="/download/spark"
      className="lb-btn lb-btn-p lb-btn-xl"
      onMouseEnter={() => light(true)}
      onMouseLeave={() => light(false)}
      onFocus={() => light(true)}
      onBlur={() => light(false)}
      onClick={(e) => trackSparkDownload(source, e.currentTarget)}
    >
      Download for Mac
    </a>
  );
}

// A phone or tablet cannot install a Mac app. iPadOS Safari calls itself a Mac, so a touch screen on a
// "Macintosh" counts too.
export function isPhoneAgent(ua: string, touchPoints: number): boolean {
  return /iPhone|iPad|iPod|Android/i.test(ua) || (/Macintosh/.test(ua) && touchPoints > 1);
}

const never = () => () => {};
// False on the server and while hydrating (crawlers and Macs get the download in the HTML), then the real
// answer in the browser.
export function useOnPhone(): boolean {
  return useSyncExternalStore(
    never,
    () => isPhoneAgent(navigator.userAgent, navigator.maxTouchPoints ?? 0),
    () => false,
  );
}

export const ON_PHONE = "Spark is a Mac app. Open this page on your Mac to download it.";

// The download button, or on a phone the line above with two ways to get the link to a Mac.
export function SparkGet({ source }: { source?: string }) {
  return useOnPhone() ? <SparkOnPhone /> : <SparkDownload source={source} />;
}

function SparkOnPhone() {
  const [copied, setCopied] = useState(false);
  // The page as opened, invite code included. Only rendered in the browser (useOnPhone is false on the server).
  const url = window.location.origin + window.location.pathname + window.location.search;
  return (
    <div className="os-phone">
      <p className="os-phone-line">{ON_PHONE}</p>
      <div className="os-phone-acts">
        <button
          type="button"
          className="lb-btn lb-btn-p"
          onClick={() => navigator.clipboard?.writeText(url).then(() => setCopied(true), () => undefined)}
        >
          {copied ? "Copied" : "Copy link"}
        </button>
        <a
          className="lb-btn lb-btn-s"
          href={`mailto:?subject=${encodeURIComponent("Spark for Mac")}&body=${encodeURIComponent(url)}`}
        >
          Email me the link
        </a>
      </div>
    </div>
  );
}
