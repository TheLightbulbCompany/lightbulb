# Resolution Center reply — guideline 3.1.1, submission 05d24a7d

Draft for review. **Not sent.** Two variants: A argues we are exempt and offers
a concrete change; B concedes and ships IAP. A is cheaper and, I think, the
stronger read — see "Why A first" below.

---

## Why A first

Apple's own text for 3.1.3(f) names the exemption class: *"Free apps acting as
a stand-alone companion to a paid web based tool (i.e. VoIP, Cloud Storage,
Email Services, Web Hosting)."* Lightbulb AI sells a private cloud container that runs
continuously on our servers — it is hosting. The subscription does not unlock
anything inside the iOS binary; it keeps a server process always-on and raises
a usage allowance. The app is free, has no IAP, no storefront, and no paywalled
screen.

3.1.3(f) has exactly one condition we currently fail: *"provided there is no
purchasing inside the app, or calls to action for purchase outside of the
app."* We have three "Upgrade on isol8.co" links. **Removing them makes the
exemption clean** — and costs us nothing an iOS user can't get on the web,
because the free tier is fully functional.

That is a one-file change, versus a StoreKit build we cannot even submit today
(the Paid Apps Agreement is still "Pending User Info").

The risk: Apple may hold that we are the primary client rather than a
"companion," and that 3.1.3(b) governs regardless. If they do, B is ready.

---

## Variant A — claim 3.1.3(f), remove the CTAs

> Hello,
>
> Thank you for the detailed review. We would like to provide additional
> information about the digital content and services in the app, as invited in
> your message.
>
> **What the subscription pays for.** Lightbulb AI is a hosted infrastructure service.
> Each customer gets a private Linux container that runs on our servers
> continuously, independently of any device. A paid plan keeps that container
> always-on and raises the monthly compute allowance. It does not unlock any
> feature, screen, or content inside the iOS app — the app is a thin client,
> and every screen in it is fully functional on our free tier with no purchase
> of any kind.
>
> **Why we believe guideline 3.1.3(f) applies.** The guideline covers free apps
> acting as a stand-alone companion to a paid web-based tool, naming Cloud
> Storage and Web Hosting as examples. That is precisely what Lightbulb AI sells:
> server-side compute and storage, provisioned and billed per account on our
> website, reachable from a web app, a macOS app, and this iOS app. What the
> customer buys is a running server, not digital content consumed in the app.
>
> **The change we are making.** Guideline 3.1.3(f) requires that there be no
> purchasing inside the app and no calls to action for purchase outside of it.
> The build you reviewed included links reading "Upgrade on isol8.co." We have
> removed every one of them. The next build contains no purchase mechanism and
> no reference to purchasing anywhere in the app.
>
> If you would still prefer that the plan be offered through In-App Purchase,
> we will implement StoreKit — we would only ask for guidance on that point
> before we submit, as our Paid Apps Agreement is still being completed and we
> cannot create subscription products until it is active.
>
> Thank you,
> Prasiddha Parthsarthy

---

## Variant B — concede, ship IAP

> Hello,
>
> Thank you for the clarification. We will make the subscription available for
> purchase inside the app using In-App Purchase, alongside the existing web
> purchase option, per guidelines 3.1.1 and 3.1.3(b).
>
> One blocker on our side: our Paid Apps Agreement is currently in "Pending
> User Info" status, so we are not yet able to create subscription products in
> App Store Connect. We are completing the banking and tax information now and
> will submit a build with the In-App Purchase once the agreement is active.
>
> Thank you,
> Prasiddha Parthsarthy

---

## If B: what ships

- Auto-renewable subscription product, one month, price TBD at product creation.
- Paywall offering IAP **and** the web option side by side (permitted on the
  U.S. storefront under 3.1.1(a), which App Review restated in this rejection).
- Backend: StoreKit 2 signed-transaction verification + App Store Server
  Notifications V2 for renew/expire/refund, writing the SAME
  `subscription_status` field the Stripe path writes, so every existing
  entitlement predicate keeps working unchanged.

Blocked on: Paid Apps Agreement → Active (bank account + tax forms).
