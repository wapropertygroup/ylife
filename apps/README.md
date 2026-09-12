# trade-agents — native app

A SwiftUI client for the existing Flask backend, building for **macOS and iOS from one
target**. There is **no new server**: every screen reads the same `/api/*` endpoints the
web dashboards do.

## Status

| | macOS | iOS |
|---|---|---|
| Swift compiles | ✅ | ✅ (`Scripts/dev.sh ios`) |
| App icon compiles | ✅ | ❌ — `actool` needs a simulator runtime |
| Runs | ✅ verified, screens below | ❌ — no runtime, no signing identity |

**macOS is the only platform this app can currently be run on**, so it is where every
visual claim here was checked. See "Environment limits" below — none of them are code
problems and all of them are outside a checkout.

### Working, verified against the live API

- **Agents** — the sampled report feed, and a full report reader. Sixteen agent turns
  per report, each with the role colour, icon and name the *server* supplies
  (`agent_roles.py`), so the app cannot drift from the web page or the completion
  email on what the cast is called. Markdown renders with tables.
- **Markets** — instrument cards with Swift Charts sparklines, the sector strip, and
  the server's freshness verdict shown rather than hidden.
- **Rates** — the FOMC path from fed funds futures: target range, per-meeting implied
  rate, and a cut/hold/hike bar with the outcome distribution for the nearest meeting.
- **Sentiment** — the fear & greed composite, its comparisons, and 273 days of history.
- **Settings** — account state, a backend switch between the two vhosts, build info.

### Not done

- **Sign-in.** The backend needs no change — `/api/auth/google` takes a Google ID token
  and replies with a session cookie, and `APIClient.signInWithGoogle` already posts it.
  What is missing is an **iOS OAuth client ID from the Google Cloud console** plus the
  GoogleSignIn SDK. Stated in the UI rather than shown as a dead button.
- **StoreKit.** Apple requires IAP for the run packs (guideline 3.1.1), which needs
  products configured in App Store Connect. The web app keeps using Stripe; the two
  purchase paths would have to agree on what a user owns.
- **A model picker on the run form.** Deliberately omitted rather than faked — see
  `APIClient.submitRun`. Offering one means a second copy of `agent_models.CHOICES` in
  Swift, including each choice's accepted thinking levels, which the server clamps.
  A stale client copy would show a level the server silently rewrote. The fix is a
  small `/api/agents/models` route exposing `agent_models.options()`, which is server
  work.
- **The other dashboards.** `/dca`, `/assets`, `/13f`, `/housing`, `/multiples`,
  `/commodities` all have JSON endpoints and no screens here yet.

## Build and run

```bash
cd apps/TradeAgents
Scripts/dev.sh run                    # build + launch (macOS)
Scripts/dev.sh shot out.png           # build, launch, screenshot the window
Scripts/dev.sh section Rates out.png  # screenshot one section of a running app
Scripts/dev.sh ios                    # type-check the iOS sources
```

Or open `TradeAgents.xcodeproj` in Xcode.

### Why `Scripts/dev.sh` screenshots the way it does

It reads the window rect, **moves the window onto the main display**, verifies both the
rect and that the app is frontmost, and only then captures. Every one of those steps is
there because its absence produced a wrong screenshot:

- `screencapture -R` takes coordinates in the *main* display's space. This machine has
  three displays and SwiftUI will happily open the window on a secondary one, at which
  point the rect is unreachable and the capture silently returns whatever is on the main
  display — twice, during development, somebody's mail client and a browser.
- `open` re-activates an already-running instance rather than starting the new build, so
  the stale-process kill has to match a **bundle-relative** path; an absolute-path
  `pkill` misses an instance launched with a relative one.
- Placing the window from *inside* the app was tried and removed: SwiftUI repositions it
  after `applicationDidFinishLaunching`, so the frame never stuck.

The capture refuses rather than guessing. A missing screenshot is obvious; a wrong one
is not.

## Environment limits

These are properties of this machine, not of the code.

- **No iOS simulator runtime** — only visionOS is installed. `actool` refuses to compile
  an asset catalog for any `iphone*` SDK without one, so an iOS *build* fails on the app
  icon even though the Swift is fine. `Scripts/dev.sh ios` type-checks instead.
  `xcodebuild -downloadPlatform iOS` is the fix and was not attempted here.
- **No code-signing identity** (`security find-identity` reports zero) and no
  provisioning profiles, so the attached iPhone cannot be deployed to.
- **The attached iPhone runs iOS 27.0**; this is Xcode 26.0.1 with the iOS 26 SDK, so
  it would need a newer Xcode for on-device debugging regardless of signing.

## Notes on the project file

`project.pbxproj` is hand-written — neither xcodegen nor tuist is installed, and a
generator would add a second source of truth. Two Xcode 16+ features keep it short:

- `PBXFileSystemSynchronizedRootGroup` — the target adopts the `TradeAgents/` folder
  wholesale, so **new `.swift` files need no pbxproj edit**. This is also why
  `TradeAgents.entitlements` sits at the *project* level: anything inside that folder is
  adopted automatically, and a non-source file would be copied into the bundle as a
  resource.
- `GENERATE_INFOPLIST_FILE` — no `Info.plist` to maintain.

**One target, two platforms.** `SDKROOT = auto` with
`SUPPORTED_PLATFORMS = "iphoneos iphonesimulator macosx"`. The iOS-only `INFOPLIST_KEY_UI*`
settings are scoped `[sdk=iphone*]`, and the entitlements file and ad-hoc signing
identity are scoped `[sdk=macosx*]`. A second target would have meant maintaining two
copies of every build setting.

`SWIFT_VERSION = 5.0` deliberately. The toolchain is Swift 6.2 and the code is written
to be concurrency-clean, but Swift 6 language mode turns actor-isolation findings into
errors; adopting it is worth doing as its own change, where the resulting diagnostics
can be read rather than fought mid-feature.

**macOS sandbox.** `com.apple.security.app-sandbox` plus
`com.apple.security.network.client`. Without the latter every request fails, and not
visibly — URLSession reports `NSURLErrorNotConnectedToInternet`, which reads as the
user's network being down rather than as a missing entitlement.

## Backend facts that shape the client

- **The showcase endpoints need no sign-in.** `api_agents_showcase` and
  `api_agents_showcase_job` in `routes.py` take no gate, which is the whole reason the
  Agents screen is a working report reader rather than a placeholder.
- **202 means two different things.** On the dashboards it is "cache warming, poll me";
  on `/api/agents/run` it is the *success* response carrying the job id. Treating it as
  warming everywhere would report a queued run — already charged — as a transient error.
- **An anonymous caller still gets a user object.** `/api/auth/me` replies
  `{"email": "", "name": "Anonymous"}`, not null, so "is there a user?" reports everyone
  as signed in. The test is a non-empty email, which is what every server-side gate uses.
- **`/api/fed` is ~1 MB** (211 KB gzipped) and `/api/commodities` ~216 KB. These are
  shaped for a desktop dashboard that renders everything at once. A phone client wants
  narrower endpoints, which is server work.
- **Nulls are the contract, not an edge case.** `pe` is null for a commodity, `prices`
  carries nulls where a session had no print, and `meta.status` is absent entirely on
  some endpoints. Rendering an absent number as `0` would be a false statement about the
  instrument, so everything formats through `Format.*`, which prints an em dash.
- **Models emit `<br>` inside table cells.** A pipe table has no other way to hold two
  lines. Both `static/markdown.js` and `report_email.py` restore it as a line break, and
  so does this app — the tag list is copied from the server's allowlist rather than
  invented, so one report renders the same in all three places.
