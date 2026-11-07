# Native mobile apps from a chat room: what it takes end to end

Date: 2026-10-29. Author: research agent for the Apps spec. Scope: an agent in a Lightbulb room builds and ships a **real native phone app** for a non-technical company, next to the Next.js + Neon app it already builds on the company's own Vercel and Neon accounts through Stripe Projects. The founder ruled that nothing is declined.

Tags: **[P]** primary (vendor docs, source code, official guidelines), **[S]** secondary (press, blogs, third-party docs, search summaries), **[U]** unverified or inferred. Every source was fetched 2026-10-29; the list is at the end. This doc does not repeat the builder summary in `2026-10-29-templates-kinds-of-apps.md` §7 or the mobile lines in `2026-10-29-builder-agents-deep.md`. Those cover Bolt's "say mobile in the first prompt", Replit's Launch to TestFlight, a0's Convex backend and OTA, Rork's switch to SwiftUI and Kotlin, Base44's web-view wrapper, and Anything's "same Expo account" rule. The "native only on a second ask" stance in those docs is overturned by the founder's ruling, and §6 replaces it.

## Short answer

- **The code is the easy part.** Every builder except Rork generates an Expo (React Native) app. Expo's cloud does the building, signing, submitting and over-the-air updates (EAS Build, Submit and Update) [P E1–E9]. An agent can drive all of it with one `EXPO_TOKEN` once the accounts are linked [P E2].
- **The accounts are the hard part, and they cannot be the platform's.** Apple rejects apps that an "app generation service" submits "on behalf of their clients"; the provider of the content must submit them (guideline 4.2.6) [P A3]. So the company needs **its own Apple Developer account** ($99 a year; an organisation needs a D-U-N-S number, a legal entity, a working website, and an account holder with 2FA and signing authority) [P A1]. For Android it also needs **its own Google Play account** ($25 once; an organisation needs D-U-N-S, which "can take up to 30 days") [P G2][P R4].
- **Each irreducible human step is a one-time step:** enroll with Apple, accept Apple's agreements, generate one App Store Connect API key, create the first app record, register with Play, invite one service account, fill in the store listings and privacy forms, and press "Submit for review". Apple's API says outright "Don't use this API to create new apps" [P A5]. EAS creates the app record only through a full Apple ID session with 2FA (eas-cli source) [P E10].
- **After that one-time setup, a release is one tap.** `eas build --auto-submit` puts a build into TestFlight and Play internal testing [P E11]. `eas update` ships JavaScript-only changes in seconds without review [P E12]. Promoting to production is one button in each store console.
- **Expo is not in the Stripe Projects catalog.** Of the 68 providers, the only mobile-relevant ones are RevenueCat and Customer.io [P E5][P E6]. Lightbulb must hold the Expo relationship itself, or ask Stripe to add Expo.
- **Recommendation (§6):** add an Expo Router `mobile/` folder to the same app repo, calling the same Next.js `/api` on Vercel. Show previews in Canvas as a web render and on phones through internal builds. Run one Expo organisation per company, with Lightbulb's robot as a member. Give staff-only tools the zero-account path first: Canvas in Lightbulb's own phone app, plus the home-screen web app.

## 1. The person's path in each builder

All builders share the same shape: preview, then the person's own store accounts, then a cloud build, then TestFlight or Play internal testing, then a listing filled in by hand, then review. They differ in how the Apple credential is captured and how much of the listing they automate.

| | Preview on a phone | Store accounts the person needs | How the Apple credential is captured | Android | After launch |
|---|---|---|---|---|---|
| **Rork** (SwiftUI / Kotlin, no Expo for new projects) | Cloud iOS simulator in the browser at "0.2 build credits/min"; "Run on your device" via Rork Companion over USB or the Rork Max iPhone app; or TestFlight [P K1] | Apple $99/yr, Google $25 once; "What Rork cannot do is be you" [P K1][P K2] | Types **Apple Developer email and password** into Rork, then enters the 2FA code [P K1][P K3] | "Connect Google Play" links the account; Rork signs with "a persistent upload key for the project", so there is no keystore to lose; automated route targets internal testing; "cannot create the first app listing" [P K2][P K4] | New Submission per version; App Store reviews arrive in the project, and the agent drafts replies [P K1] |
| **Replit** (Expo) | iOS Simulator / Android Emulator in Preview; Expo Go QR [P R1] | Apple membership with 2FA [P R1] | "Sign in with Apple" in the Launch wizard; "does not store your password" [P R2] | Not in the guides fetched; earlier note says Google Play not supported [P R1] | Publish the web backend first: "PixelGames' backend needs to be live before the App Store launch" [P R2] |
| **Anything** (Expo) | Device frame in browser; Expo Go after pasting a **personal Expo access token** ("not a CI or robot account") [P Y1] | Apple; Google Play in beta [P Y1][P Y3] | Apple ID + text or device 2FA code; build and upload "usually takes 30 minutes or less" [P Y2] | Person creates the Play app with the exact package name, then **invites Anything's service account** with "Release apps to testing tracks"; uploads as draft to Internal testing [P Y3] | Agent swaps App Store screenshots in App Store Connect for you [P Y2]; a public **API with "Start an App Store submission"** endpoints for agents [P Y5] |
| **a0.dev** (Expo, Convex) | Web preview via react-native-web; the a0 app or Expo Go; native builds for payments [P Z1] | Apple with **Admin** role [P Z2] | **Team API key (.p8) with Admin access**: "Automatic Setup" or manual upload of Key ID, Issuer ID, Team ID [P Z2][P Z3] | Not documented in the pages fetched | Built-in OTA "without App Store review times" [P Z5] |
| **Bolt** (Expo) | "Device Preview" Expo Go QR; first time "will take some time to build" [P B1] | Expo, Apple, Google Play accounts; "A computer with Node.js LTS and Git" [P B1] | None: the person downloads the code and runs `eas login`, `eas build` locally [P B1] | Same, by hand | By hand |
| **Lovable** | Responsive preview only | n/a | n/a | n/a | "Not as native apps… it does not generate React Native projects"; points to PWA or Capacitor "outside Lovable" [P L1] |

What a non-technical person still does in every builder (from the builder's own docs):

1. **Enroll with Apple, days ahead.** "New Apple Developer Accounts typically take 3 days to process and may require ID verification" [P Y2]. Rork: "Start this early" [P K1].
2. **Check roles and agreements.** Only Account Holder or Admin "can sign builds and publish apps". The Paid Apps Agreement, banking and tax must be "Active" even for a free app with in-app purchases. "Team and Provider must match" [P Y4].
3. **Fill in the listing by hand in App Store Connect.** This covers screenshots at the required sizes, description, contact info, a demo account "if your app requires sign-in", a privacy policy URL and a support URL [P Y2][P R3].
4. **Pass TestFlight beta review on the first build** before external testers can install it [P R1][P A6].
5. **On Google Play, finish the listing and the policy forms by hand.** These are the data safety form, content rating and target audience. A personal account created after 13 Nov 2023 must run a closed test with "at least 12 testers opted in continuously for 14 days" [P G1][P K2][P Y3].

The builders' pre-review tools exist because rejections are common. Rork's **AI App Store Reviewer** reads the build against guideline sections 1–5 and reports "Likely to pass / Likely rejection / Outcome uncertain". It is "advisory", allows 40 runs a day, and fixes are made in chat one finding at a time (example: 5.1.1 account deletion) [P K5]. Anything has an "App Store Review" check in Publish [P Y2]. Replit's checklist covers account deletion, user-generated-content moderation (filter, report, block), a live privacy policy, a security scan, and "Using a native framework alone does not satisfy Apple's minimum functionality requirements" [P R3]. Rork's top rejection reasons are permission strings that don't explain the need, hard paywalls not disclosed in the description, and stretched iPhone screenshots used as iPad screenshots [P K6].

## 2. The machinery: Expo, EAS, and what an agent can do alone

### 2.1 Templates

`npx create-expo-app` `default` is Expo Router + TypeScript with tabs; `blank`, `blank-typescript`, `tabs`, `bare-minimum` also exist (covered in the templates doc §7). Lightbulb's own phone app already runs Expo SDK 57 with `expo-router`, `expo-updates`, `expo-notifications`, `expo-secure-store`, `expo-web-browser` and `react-native-webview`. Its `eas.json` has `development`, `preview` (internal distribution) and `production` profiles under the `thelightbulbcompany` Expo owner (`apps/mobile/package.json`, `apps/mobile/eas.json`, `apps/mobile/app.json:5`) [P repo]. The team already runs this stack.

### 2.2 Pricing and free-tier limits

| Plan | Price | Builds | macOS concurrency | Timeout | EAS Update MAU | Hosting |
|---|---|---|---|---|---|---|
| Free | $0 | "up to 15 Android and 15 iOS builds", low-priority queue | 1 | 45 min | 1,000 | 100k requests/mo, no custom domain |
| Starter | $19/mo + usage | $45 build credit | 2 | 2 h | 3,000 | 100k then $2/1M; 1 custom domain/project |
| Production | $199/mo + usage | $225 build credit | 10 | 2 h | 50,000 | as Starter |
| Enterprise | custom | from $1,000 credit | 15 | 2 h | volume | as Starter |

Overage per build: Android $1 (medium) or $2 (large); iOS $2 or $4. EAS Update overage is $0.005 per MAU. Cloud simulators (beta) cost $0.10 a minute, with 60 minutes a month included on paid plans "until 2027" [P E1].

For Lightbulb, a small company shipping weekly needs about 8 builds a month, which fits the free tier. The binding limits are the **low-priority queue**, with a person waiting in a room, and **1,000 OTA monthly active users**. Starter at $19 a month removes both for most small companies [P E1]. The free queue wait is not published [U].

### 2.3 EAS Build and who holds the signing keys

- EAS generates and stores the iOS distribution certificate, provisioning profile and push key, and the Android keystore, "on EAS servers", and reuses them on later builds. Generating the iOS set "requires you to sign in with an Apple Developer Program membership" [P E7].
- So **whichever Expo account owns the project holds the signing keys**. Replit's Launch "selects a valid distribution certificate and provisioning profile automatically, or creates new ones" [P R2]. Rork holds "a persistent upload key for the project" [P K4]. If a builder holds the keys, leaving the builder means a key reset. Google Play App Signing can reset a lost upload key; Apple certificates can simply be re-issued [U].
- **Internal distribution** is the preview channel for real builds. iOS ad hoc builds carry "an allow-list of device UDIDs" added with `eas device:create`, so every tester's phone must be registered before the build. Android internal builds are APKs installable "by downloading the file over the web". Each build gets a URL, which by default anyone with the link can open [P E13]. Every iPhone build "require[s] a paid Apple Developer account for build signing" [P E14].

### 2.4 EAS Submit: what must exist first

- **iOS** needs an **App Store Connect API key**: set once via `eas credentials --platform ios`, or `ascApiKeyPath`/`ascApiKeyIssuerId`/`ascApiKeyId` in `eas.json`. In `--non-interactive` mode, "App Store Connect API Keys cannot be set up" and `ascAppId` must be present. Builds reach TestFlight in "usually 10-15 minutes", and a prebuilt `testflight` workflow job shares them with tester groups. App Store release still means "submit the build for App Review in App Store Connect" [P E8].
- **Who can make the key.** "The Account Holder must request access to the API", and requests are "reviewed and approved on a case-by-case basis". Team keys can be generated by Account Holder or Admin, apply "across all apps", and "can't edit its name or access level" afterwards [P A2]. a0 asks for a team key with **Admin** access and warns that you can download the .p8 "only once" [P Z3].
- **The app record cannot be created with the API key.** Apple: "Don't use this API to create new apps; instead, create new apps on the App Store Connect website" [P A5]. eas-cli works around this with `ensureAppExistsAsync`, which uses an **Apple ID user session** (`@expo/apple-utils` `Session`), not the API key. It also creates the bundle ID if the user has `provisioningAllowed`, plus an internal TestFlight group [P E10]. So the first app needs one Apple ID sign-in with 2FA, or one manual "New App" in App Store Connect (a0's guide has the person create the bundle ID and app entry by hand [P Z4]).
- **Android** needs a **Google Service Account key**, uploaded once and reusable across the account's apps. Non-interactive runs fail unless a key is assigned. The default `eas submit` "creates the first release automatically" on the **internal** track, and the app "stays in draft status in Play Console until the store listing and setup tasks are complete" [P E9]. Anything's variant has the customer invite **the platform's** service account email into their Play Console, so the platform keeps no customer key file [P Y3].
- `eas build --auto-submit` chains build and submit. iOS "goes to TestFlight, not App Store review"; new Android apps "get an internal release" [P E11].

### 2.5 EAS Update and EAS Hosting

- **Update** changes "non-native pieces (such as JS, styling, and images)". Native code, native dependencies, permissions and SDK version need a new binary. Runtime versions keep incompatible updates off old builds. Channels (`eas update --channel production`), percentage rollouts, and rollback by republishing are supported. Updates "need to follow the App Store and Play Store guidelines" [P E12]. Apple's 2.5.2 forbids downloaded code "which introduces or changes features or functionality" [P A3]. OTA is therefore for fixes and copy, not for slipping in new features past review [U: Apple's enforcement line on JS bundles is not published].
- **Preview links.** `eas update --auto` prints a dashboard link, and reviewers click **Preview** and "scan to open the preview on their device" [P E15]. An EAS Workflow can publish an update on every push to every branch [P E15].
- **Hosting** deploys Expo Router web output and API routes on Cloudflare Workers ("partial" Node.js support), aimed at keeping server versions in sync with store releases [P E16]. Lightbulb does not need it: the API already lives on Vercel.

### 2.6 Who can drive EAS without a human

- `EXPO_TOKEN` drives every EAS CLI command and "takes precedence over the username and password". A token run needs the project linked: `eas init --force --non-interactive` [P E2].
- **Personal access tokens** work across every account and organisation the user can reach. **Robot users** act on resources the owning account holds, are role-limited, and "cannot sign in to any Expo products, cannot own any projects themselves" [P E2].
- **Organisations** have roles Owner > Admin > Release Manager > Developer > Viewer. Developer "creates projects, makes builds, releases updates, and manages credentials". Projects can be **transferred** a limited number of times between accounts where the user is Owner/Admin on both, using an "escrow Organization" when needed [P E17].
- **No documented project-creation or account-creation API.** `eas init` is the documented path [P E2]. No Expo programme for AI builders creating accounts for end users turned up. Expo's MCP server requires "an EAS paid plan" [S E3]. StackOne exposes a "create app" endpoint over Expo's API, which suggests Expo's GraphQL API can do it [S E4].
- **Rule of thumb.** Everything inside Expo can be automated with a token. Every step that touches Apple's or Google's identity cannot: membership, agreements, API key, first app record, Play registration, Play listing and policy forms, review submission answers.

### 2.7 Stripe Projects does not carry Expo

The provider table (68 on projects.dev) has **no Expo, EAS, Apple, Google Play, Firebase or OneSignal**. The only mobile-relevant entries are **RevenueCat** ("in-app subscriptions for iOS, Android, and web") and **Customer.io** (push and in-app messages). Stripe takes requests at provider-request@stripe.com [P E5][P E6]. The Expo account therefore cannot be minted on the same `stripe projects add` rail as Vercel and Neon today.

## 3. Sharing the backend with the web app

- **One API, two clients.** The Expo app calls the Next.js routes on Vercel by absolute URL, set as `EXPO_PUBLIC_API_URL` per EAS build profile. Lightbulb's own phone app already does exactly this (`apps/mobile/eas.json`) [P repo]. Native `fetch` is not subject to browser CORS, so the existing `/api` works unchanged; only cookie-based auth needs a bearer-token path [U].
- **Auth on mobile.**
  - `expo-auth-session` builds redirects with `makeRedirectUri({ scheme })` (e.g. `your.app://redirect`). **Expo Go "can't be used to test OAuth"** because its scheme can't be customised, so a development build is needed [P E18].
  - Tokens go in `expo-secure-store` (Keychain on iOS, encrypted SharedPreferences on Android), never AsyncStorage, and codes are exchanged on the server so "client secrets never ship in the app" [P E18].
  - For Lightbulb's "door" (a signed pass from Lightbulb, the room is the login; Apps spec §4), the mobile equivalent is: open Lightbulb's sign-in in an auth session, redirect to `companyapp://door?pass=…`, have the app's `/api` verify the pass and return a session token, and store it in SecureStore [U: design inference].
  - Apple's 4.8 exempts "business app[s] that require the user to sign in with an existing… enterprise account" from offering Sign in with Apple [P A3].
  - Any app that lets people *create* accounts must "offer account deletion within the app" (5.1.1(v)) [P A3].
- **Push.**
  - The client gets `getExpoPushTokenAsync({ projectId })`; the token is a UUID-bound value that survives account transfers. The server POSTs to `https://exp.host/--/api/v2/push/send` [P E19].
  - iOS needs an APNs key, which EAS generates during the first build with a paid Apple account. Android needs FCM V1 credentials [P E19]: a Firebase project and a service-account JSON uploaded to EAS [U: from the linked guide, not fetched].
  - Push does not arrive in Expo Go on iOS, so test from TestFlight [P R4].
  - The web app's server sends to Expo push tokens stored in Neon next to the user row. One table, one sender, both clients [U].
- **Payments.**
  - **Physical goods or services used outside the app** (bookings, deliveries, field work, invoices) "must use purchase methods other than in-app purchase", so Stripe is correct (3.1.3(e)) [P A3].
  - **Digital features or subscriptions** "must use in-app purchase" (3.1.1). RevenueCat is in Stripe Projects and is what Replit, Rork and Anything use [P A3][P E5][P K7][P Y1].
  - **Enterprise exception:** apps "only sold directly by you to organizations or groups for their employees" may let enterprise users access previously purchased content or subscriptions (3.1.3(c)). B2B SaaS companions bought on the web are fine if the app sells nothing [P A3].
  - **US storefront:** links and buttons to outside purchase are allowed ("where this prohibition does not apply") [P A3]. After the Dec 2025 Ninth Circuit ruling, Apple may seek "a reasonable commission" on linked purchases, and that rate was still being set in district court in 2026 [S N1][S N2].
  - Default for Lightbulb: Stripe on the web app; the phone app shows status and links out only in the US, or sells nothing in the app.

## 4. The installable web app (PWA) as a complement

- **Home screen.** In iOS/iPadOS 26, any site added from Safari's share sheet "open[s] as a web app" by default, with "zero conditions"; no manifest is needed, though its icons are used if present [S W2][S W3].
- **Push.** Web Push for Home Screen web apps has worked since iOS 16.4. The permission request must answer "direct user interaction", the Badging API works, and "You do not need to be a member of the Apple Developer Program" [P W1].
- **Offline.** Service workers are not required on iOS but "can significantly improve the user experience" [S W3].
- **What it can't do** [U, standard platform limits not re-fetched this session]: App Store presence, background location, Bluetooth/NFC, rich camera control, widgets, Sign in with Apple native sheet, Apple Pay in-app sheet parity, and reliable background sync.
- **When it is enough:** staff tools (schedules, checklists, CRUD, dashboards, intake forms) used by people the company can send a link to. The app is the same Next.js deployment with no new account, and push reaches iOS once the icon is on the home screen.
- **When it is not:** customer-facing apps that must be found in a store, apps needing hardware or background work, and apps that sell digital goods.

## 5. App Store review risks for AI-generated apps

| Guideline | Text | Risk for Lightbulb | Mitigation |
|---|---|---|---|
| **4.2.6** | Apps from "a commercialized template or app generation service will be rejected unless they are submitted directly by the provider of the app's content. These services should not submit apps on behalf of their clients" [P A3] | Fatal if Lightbulb submits from its own Apple account | Always the **company's own Apple account**, as every builder does [P K1][P Y2][P R2] |
| **4.2 / 4.2.2** | "elevate it beyond a repackaged website"; not "web clippings… or a collection of links" [P A3] | A WebView of the Next.js app fails | Native screens (lists, forms, camera, push, offline); Replit: "native framework alone does not satisfy" [P R3] |
| **4.3(a)/(b)** | No "multiple Bundle IDs of the same app"; nothing "indistinguishable from what's already widely available" [P A3] | Low per company (each is its own business app); high if one starter ships unchanged to many companies under one account | One company per account by construction; brand, data and flows differ |
| **2.1** | No placeholders; "include demo account info (and turn on your back-end service!)" [P A3] | The door sign-in blocks the reviewer | Generate a reviewer account automatically on submit; Anything and Replit ask for this [P Y2][P R3] |
| **5.1.1(v)** | Account deletion in-app if accounts can be created [P A3] | Customer-facing apps | Generated by default in the template |
| **2.5.2** | No downloaded code that "introduces or changes features" [P A3] | Aggressive OTA | OTA for fixes only; feature releases go through a new build |
| **4.8** | Sign in with Apple when using third-party social login [P A3] | Customer apps using Google login | Business apps on an enterprise login are exempt [P A3] |

Two lower-risk routes for staff-only tools:

- **Unlisted distribution:** the app is on the App Store but "discoverable only through a direct link", for "employees, franchisees, partners". It still goes through App Review, with a note in Review Notes, plus a separate request form [P A7].
- **Custom apps** via Apple Business Manager: private to named organisations, distributed by MDM or redemption codes, and still reviewed [P A8].

## 6. Recommendation for Lightbulb

### 6.1 Three tiers, never a refusal

1. **Tier 0: no new account (minutes).** Every Lightbulb app is already a responsive Next.js app. Staff open it from **Canvas in the Lightbulb phone app**, or add it to the home screen as a web app with Web Push. This covers most internal tools.
   - The Lightbulb app already ships `react-native-webview` (`apps/mobile/src/components/room/AttachmentViewerModal.tsx:19`) [P repo]. A Canvas screen there is a Lightbulb feature, not a store submission.
   - Apple's 4.7 allows "HTML5 and JavaScript mini apps" inside an app, provided the host takes responsibility for them [P A3].
   - Not yet built: Canvas is not in `apps/mobile` today [P repo].
2. **Tier 1: a real native app for staff (days, company's Apple account).** An Expo app in the same repo, distributed by TestFlight internal testing (up to 100 team members, no review), external TestFlight (up to 10,000 testers, first build reviewed) [P A6], or unlisted distribution [P A7].
3. **Tier 2: a public store app.** The same Expo app, with listings, review, and Play's production track.

The agent says which tier fits and starts building at once; the founder ruling means tier 2 is always available.

### 6.2 The stack

- **Repo layout:** `web/` (the existing Next.js starter) plus `mobile/`, an Expo Router `default` template on the same SDK line as Lightbulb's own app [P repo]. There is no shared UI package: the phone screens are native, and only `lib/api-types.ts` (zod schemas from the API routes) is shared [U: design].
- **API:** the Next.js `/api` on Vercel, with `EXPO_PUBLIC_API_URL` set per profile to the preview or production deployment [P repo pattern].
- **Auth:** the door, extended with a mobile redirect (§3), a bearer token in SecureStore, and an auto-created reviewer account. Customer-facing apps add sign-up and in-app account deletion.
- **Push:** `expo-notifications` and an Expo push token column in Neon. The web server sends through Expo Push.
- **Payments:** Stripe (physical goods and services, B2B, US link-out). RevenueCat from Stripe Projects only when digital goods are sold in the app.
- **`app.yaml`:** gains `mobile: { bundle_id, android_package, expo_project_id }`. The bundle ID is chosen once, from the company domain, and never changed, because "cannot be changed after the first build" [P R2][P B1].

### 6.3 Who owns which account

| Account | Owner | How Lightbulb gets access |
|---|---|---|
| Apple Developer Program (org) | The company; its owner is Account Holder | One **Team API key (Admin)** pasted into Lightbulb once, as a0 does [P Z3]. Stored in Secrets Manager under the app, like Vercel/Neon credentials (Apps spec §6) |
| App Store Connect app record | The company | Created once: one Apple ID sign-in with 2FA during the first submit (eas-cli's path [P E10]), or the person clicks "New App" with the bundle ID Lightbulb shows |
| Google Play Console (org) | The company | The company invites **Lightbulb's service account email** with "Release apps to testing tracks", as Anything does [P Y3]. No key file leaves Google |
| Expo organisation | The company, ideally | Lightbulb's robot user is a **Developer** member [P E17][P E2]. Until the company has an Expo login, Lightbulb creates the project in a Lightbulb-held escrow organisation and transfers it when the company wants it [P E17]. Signing keys live there, so ownership must end with the company |
| Firebase project (FCM) | The company's Google account | Created by the company owner, or by Lightbulb through the company's Google Cloud connection [U] |
| RevenueCat (only if digital goods) | The company | Stripe Projects, same rail as Neon/Vercel [P E5] |

Ask Stripe to add Expo to Projects (provider-request@stripe.com [P E5]). Until then, Lightbulb pays the Expo free/Starter plan per company, or the company's own card sits on its Expo organisation.

### 6.4 The agent workflow in a room

1. **"Make it a phone app."** The agent runs `lightbulb app mobile add`: it scaffolds `mobile/` and links the Expo project (`eas init --non-interactive` with `EXPO_TOKEN` [P E2]).
2. **Preview in Canvas, no install:** an Expo web export of the phone app in a phone frame, like Anything's device frame and a0's react-native-web preview [P Y1][P Z1]. Cheapest and instant.
3. **Preview on a phone, before any Apple account:**
   - **Android:** an internal APK link posted in the room installs directly [P E13].
   - **iOS:** Expo Go loads an EAS Update QR from the room. Expo Go is officially a "playground… not useful for building production-grade projects" [P E20]. It also cannot do OAuth redirects [P E18] or push [P R4], so the door must offer a dev-only pass link in preview [U].
4. **Preview on an iPhone, after Apple:** the agent posts a TestFlight internal link once the first build is processed. Each later code change becomes an `eas update` to the `preview` channel, so testers see it on next launch without a rebuild [P E12][P E8].
5. **"Ship it":**
   - `eas build --platform all --profile production --auto-submit` puts the build into TestFlight and Play internal testing [P E11].
   - The agent fills in what the APIs allow: screenshots captured from the simulator, description, privacy and support URLs from the web app, review notes with the reviewer account. Anything already pushes screenshots into App Store Connect from chat [P Y2].
   - It then posts one card: "Submit for review". The App Store Connect API can submit a version for review [U: endpoint exists in Apple's API, not fetched this session].
6. **Fixes after launch:** JS-only fixes go out by `eas update --channel production` with a rollback; native changes trigger a new build and resubmit [P E12].
7. **Run a pre-review check** like Rork's against guidelines 2.1, 4.2, 4.2.6, 5.1.1(v) and 4.8 [P K5]. Post findings in the room before submitting.

### 6.5 The irreducible human steps, shortest path through each

| Step | Who | Shortest path | Time |
|---|---|---|---|
| D-U-N-S number | Company owner | Look up or request free from Dun & Bradstreet via Apple's lookup tool [P A1]; Google: "can take up to 30 days" [P G2] | Days–weeks; start on the first mobile ask |
| Apple Developer enrollment (org, $99/yr) | Owner with signing authority, 2FA on, legal name, work email on company domain, live company website [P A1] | Lightbulb posts a checklist card the moment someone says "phone app" | ~3 days typical [P Y2][P Y4] |
| Accept agreements; Paid Apps + tax/banking only if selling | Account Holder | One link to Agreements, Tax, and Banking [P Y4] | Minutes |
| Request API access + generate Admin team key | Account Holder (request), Admin (key) [P A2] | Deep link to `appstoreconnect.apple.com/access/integrations/api`; paste Key ID, Issuer ID, .p8 into Lightbulb [P Z3] | Minutes, after Apple approves access |
| First app record | Admin | One Apple ID sign-in with 2FA during the first submit, or "New App" with values Lightbulb shows [P E10][P A5] | Minutes |
| Google Play registration ($25, org) | Owner | Same D-U-N-S; verify contacts by OTP [P G2][P K2] | Up to days |
| Create Play app + invite service account | Owner/Admin | Paste the package name Lightbulb shows; invite Lightbulb's service account email [P Y3] | Minutes |
| Store listing, privacy/data-safety, content rating | Owner reviews; agent drafts | The agent pre-fills everything the APIs accept; the human confirms the legal answers | 15–30 min |
| 12 testers × 14 days (personal Play accounts only) | Company | Avoid with an organisation account [P G1]; the exemption for orgs is only confirmed by secondary sources [S G3] | 14 days if hit |
| Submit for review / answer reviewer messages | Account Holder or Admin | One tap in the room card; reviewer questions relayed to the room for a human answer | Review time not published by Apple [U] |

### 6.6 Open questions for the founder

1. Who pays Expo: the company (its own Expo organisation and card) or Lightbulb (bundled)? This changes who holds the signing keys.
2. Should Canvas in the Lightbulb phone app (tier 0) be built first? It needs no customer account at all and covers most staff tools.
3. Ask Stripe for an Expo provider in Projects, or build an Expo GraphQL integration ourselves?

## Sources (all fetched 2026-10-29)

**Expo.**
- E1 https://expo.dev/pricing
- E2 https://docs.expo.dev/accounts/programmatic-access/
- E3 web search summary of https://docs.expo.dev/eas/ai/mcp.md and https://expo.dev/ai
- E4 https://www.stackone.com/connectors/expo/ (via search)
- E7 https://docs.expo.dev/app-signing/managed-credentials/
- E8 https://docs.expo.dev/submit/ios/
- E9 https://docs.expo.dev/submit/android/
- E10 https://raw.githubusercontent.com/expo/eas-cli/main/packages/eas-cli/src/submit/ios/AppProduce.ts
- E11 https://docs.expo.dev/build/automate-submissions/
- E12 https://docs.expo.dev/eas-update/introduction/
- E13 https://docs.expo.dev/build/internal-distribution/
- E14 https://docs.expo.dev/develop/development-builds/introduction/
- E15 https://docs.expo.dev/review/share-previews-with-your-team/
- E16 https://docs.expo.dev/eas/hosting/introduction/
- E17 https://docs.expo.dev/accounts/account-types/
- E18 https://docs.expo.dev/guides/authentication/
- E19 https://docs.expo.dev/push-notifications/push-notifications-setup/
- E20 https://docs.expo.dev/workflow/expo-go/

**Stripe.**
- E5 https://docs.stripe.com/projects
- E6 https://projects.dev/providers

**Apple.**
- A1 https://developer.apple.com/programs/enroll/
- A2 https://developer.apple.com/help/app-store-connect/get-started/app-store-connect-api/
- A3 https://developer.apple.com/app-store/review/guidelines/ (2.1, 2.5.2, 3.1.1, 3.1.1(a), 3.1.3(c)(e)(f), 4.2, 4.2.2, 4.2.3, 4.2.6, 4.3, 4.7, 4.8, 5.1.1(v))
- A5 https://developer.apple.com/tutorials/data/documentation/appstoreconnectapi/apps.json ("Don't use this API to create new apps")
- A6 https://developer.apple.com/testflight/
- A7 https://developer.apple.com/support/unlisted-app-distribution/
- A8 https://developer.apple.com/custom-apps/

**Google.**
- G1 https://support.google.com/googleplay/android-developer/answer/14151465
- G2 https://support.google.com/googleplay/android-developer/answer/13628312
- G3 web search summary (dev.to, testerscommunity.com, choicely.com) on the organisation exemption

**Web platform.**
- W1 https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/
- W2 MacRumors / iDownloadBlog via search (iOS 26 "Open as Web App")
- W3 https://heise.de/-10749652

**US payments case.**
- N1 https://www.macrumors.com/2025/12/11/apple-app-store-fees-external-payment-links/ (via search)
- N2 https://macrumors.com/2026/04/29/epic-games-wins-reversal-app-store-fee-battle/ and https://macrumors.com/2026/06/11/apple-fights-back-against-epic/ (via search; Supreme Court outcome after June 2026 not found)

**Rork.**
- K1 https://docs.rork.com/iphone/rork-iphone.md
- K2 https://docs.rork.com/store-guides/google-play-developer-account.md
- K3 https://docs.rork.com/publishing.md
- K4 https://docs.rork.com/android/rork-android.md
- K5 https://docs.rork.com/ai-app-store-reviewer.md
- K6 https://docs.rork.com/appstore-submission-checklist/whats-next-after-the-apps-finished.md
- K7 https://docs.rork.com/llms.txt

**Replit.**
- R1 https://docs.replit.com/build/mobile-app.md
- R2 https://docs.replit.com/build/mobile-upload-ios.md
- R3 https://docs.replit.com/build/mobile-publish-ios.md
- R4 https://docs.replit.com/build/mobile-push-notifications.md

**Anything.**
- Y1 https://www.anything.com/docs/apps/mobile.md
- Y2 https://www.anything.com/docs/launch/app-store.md
- Y3 https://www.anything.com/docs/apps/mobile/publishing-android.md
- Y4 https://www.anything.com/docs/apps/mobile/verify-apple-developer-account.md
- Y5 https://www.anything.com/docs/llms.txt (API reference: "Start an App Store submission", "Get Play Store setup details")

**a0.dev.**
- Z1 https://docs.a0.dev/llms.txt
- Z2 https://docs.a0.dev/publishing/ios/app-store-connect/automatic-setup.md
- Z3 https://docs.a0.dev/publishing/ios/app-store-connect/manual-setup.md
- Z4 https://docs.a0.dev/publishing/ios/preparing-asc-account.md
- Z5 https://docs.a0.dev/publishing/ota-updates/overview.md

**Bolt and Lovable.**
- B1 https://support.bolt.new/integrations/expo.md
- L1 https://docs.lovable.dev/introduction/faq.md

**Repo.** `apps/mobile/package.json`, `apps/mobile/eas.json`, `apps/mobile/app.json`, `apps/mobile/src/components/room/AttachmentViewerModal.tsx` in lightbulb.nosync, read 2026-10-29.
