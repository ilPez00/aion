# Installing the AION HUD on a phone (PWA → APK)

The AION web HUD (`scripts/aion_web.py` serving `scripts/static/`) is now a
**PWA**: manifest + service worker + icon. There are two levels of "install".

## Reality check

- AION has **no native Android app** and no Android build toolchain in-repo.
- The HUD is a responsive web app. It installs as a PWA, and a real signed
  `.apk` can be produced from that PWA with Bubblewrap — but only with an
  Android SDK and the PWA served over **public HTTPS**.

## The one blocker: HTTPS

Service workers (what makes a PWA installable + offline) require a **secure
context** — HTTPS, or `localhost`. The daemon serves plain HTTP on the LAN, so
from a phone at `http://<host>:8742` the SW will **not** register and Chrome
won't offer "Install app". Fix it once, and both PWA-install and APK-build work:

- **Tailscale Serve** (easiest): `tailscale serve https / http://127.0.0.1:8742`
  → gives a stable `https://<machine>.<tailnet>.ts.net` URL.
- **cloudflared**: `cloudflared tunnel --url http://127.0.0.1:8742`
  → a `https://*.trycloudflare.com` URL.
- **Self-signed TLS** on the daemon (LAN-only; phone must trust the cert).

## Level 1 — install as a PWA (no APK, installs today)

1. Serve the HUD over HTTPS (above).
2. Open the HTTPS URL in Chrome/Edge on Android (Safari on iOS).
3. Menu → **Install app** / **Add to Home Screen**.
4. Launches standalone (own icon, no browser chrome, offline shell cached).

This is the fastest "app on my phone" and needs no Android SDK.

## Level 2 — build a real `.apk` (Bubblewrap / TWA)

A Trusted Web Activity wraps the hosted PWA in a signed Android package.
Prerequisites (run where they exist — not in this session):

- Node 18+, JDK 17, Android SDK (`ANDROID_HOME` set).
- The PWA reachable at a public HTTPS URL (your tunnel above).

Steps:

```bash
npm i -g @bubblewrap/cli
cd android/                       # a build dir of your choosing
cp <repo>/twa-manifest.json .     # scaffolded config — edit the host first
bubblewrap init --manifest ./twa-manifest.json
bubblewrap build                  # produces app-release-signed.apk + .aab
```

Then complete the trust link so the app opens without a browser bar: Bubblewrap
prints an `assetlinks.json`; serve it at
`https://<your-host>/.well-known/assetlinks.json`. Add a route for it in
`aion_web.py` (mirror the `/manifest.webmanifest` route) or have the tunnel
serve the file.

`sideload the apk`: `adb install app-release-signed.apk`.

## Files added for this

- `scripts/static/manifest.webmanifest`, `sw.js`, `icon.svg` — the PWA.
- Root routes in `scripts/aion_web.py` (`/manifest.webmanifest`, `/sw.js`,
  `/icon.svg`) so the service worker owns the `/` scope.
- `twa-manifest.json` — Bubblewrap config; set `host`/`startUrl`/`webManifestUrl`
  to your HTTPS URL before `bubblewrap init`.
