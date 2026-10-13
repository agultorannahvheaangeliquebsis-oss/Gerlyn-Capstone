# GerlynPLUS Desktop (Windows)

An Electron shell around the live GerlynPLUS site. It is **not** a second copy
of the system: the pages, sign-in, roles, module permissions, branch scoping
and the database all stay on the Railway server. The browser version is
unchanged and fully functional.

What the desktop app adds:

| | |
|---|---|
| Installable app | `GerlynPLUS` name, icon, Start Menu and Desktop shortcut |
| Pinned to one server | The window can only show your GerlynPLUS server; other links open in the default browser |
| Local front-end resources | The site's CSS/JS/images, the icon script and Roboto are bundled in the installer, so pages render without internet |
| Offline launch | If the server cannot be reached, a local page lists the saved pages and any changes waiting to sync, and returns to the app on its own |
| Offline work | The web app's own offline queue (see *Offline mode* in the root README) runs unchanged inside the shell |
| Hardened shell | No Node in pages, camera only (barcode scanner), no other permissions, single instance |

## How offline works (and why there is no second queue)

The web app already has an offline layer: a service worker that keeps saved
copies of Inventory / Sales / Procurement pages, an IndexedDB outbox in the
page, and `/offline/sync/`, which replays each queued change through the real
view with an operation-id ledger so it is applied exactly once.

The desktop app loads those same pages, so it gets that behaviour — including
its limits (only the five allow-listed change types can be queued; approvals,
payment verification, refunds, settings, reports, etc. stay online-only).
Adding a separate desktop database would create a second source of truth and a
second place a sale could be recorded twice, so it deliberately does not.

What the shell guarantees on top:

- **No duplicates** — the queue and server ledger are the web app's. The shell
  also allows only one running copy per Windows user, so one outbox has one
  sender.
- **No cross-account sync** — a queued change carries the account that made
  it; another account signed in on the same computer neither sees nor sends it,
  and the server refuses it anyway.
- **No silent success** — a failed request reaches the page as a failed
  request. The shell never invents a response for a document, API call or
  form post; only static files (CSS/JS/images/fonts) are ever answered locally.
- **Data survives restart and uninstall** — queued changes live in the app's
  profile (`%APPDATA%\GerlynPLUS`), which the uninstaller leaves in place.

Sign out at the end of a shift on a shared computer: it clears the saved
pages (the web app's service worker does this at `/logout/`).

## Requirements

- Windows 10/11, x64
- Node.js 20 or newer (22 LTS recommended) and npm
- Network access during `npm install` and `npm run bundle` (downloads Electron,
  and mirrors the icon script and Roboto once)

## Build

```powershell
cd desktop
npm install

# The address the installer should default to (optional -- if omitted the app
# asks for it on first run).
$env:GERLYN_SERVER_URL = "https://your-app.up.railway.app"

npm run dist        # NSIS installer  -> dist\GerlynPLUS-Setup-<version>.exe
npm run dist:msi    # Windows Installer -> dist\GerlynPLUS-Setup-<version>.msi
npm run pack        # unpacked app only -> dist\win-unpacked\GerlynPLUS.exe
```

`npm run bundle` (run by all of the above) copies the static files and mirrors
the third-party assets into `desktop/bundled/`. Static files come from
`../staticfiles` if you have run `collectstatic` (hashed names, ideal for a
release), otherwise from `../static`. **Build releases from a committed tree** —
the bundle is whatever is in your working copy.

Run without installing: `npm start` (set `GERLYN_SERVER_URL` first, or enter it
in the first-run screen).

### Smart App Control / "spawn UNKNOWN"

`npm run dist` makes electron-builder compile and **execute** a small unsigned
helper to write the uninstaller. On a PC with Windows *Smart App Control*
enforced, Windows refuses to run it and the build stops with
`spawn UNKNOWN` (Event Viewer → *CodeIntegrity* shows "Smart App Control
Block"). Options, in order of preference:

1. Build on another machine or in CI. `.github/workflows/desktop-windows.yml`
   does this on a clean Windows runner (see *Automatic builds* below).
2. Use `npm run dist:msi` — the WiX tools are signed, so it builds under Smart
   App Control.
3. Turn Smart App Control off. This is a one-way switch in Windows; not
   recommended on a work PC.

### Code signing

Installers are **unsigned**, so Windows SmartScreen shows "Windows protected
your PC" on first run (More info → Run anyway). To remove that, sign with an
Authenticode certificate: set `CSC_LINK` and `CSC_KEY_PASSWORD` before
`npm run dist` (see electron-builder's code-signing docs).

## Pointing the app at a server

Resolved in this order:

1. `GERLYN_SERVER_URL` environment variable (locks the setting)
2. The first-run screen / **File → Server Address…** (saved per user)
3. The address baked in at build time (`GERLYN_SERVER_URL` during `npm run bundle`)

Only `https://` is accepted, except `http://` for `localhost` and private
network addresses (a LAN install). Note that browsers only allow service
workers on https or localhost, so a plain-http LAN server runs without the
saved-page cache.

## Automatic builds

`.github/workflows/desktop-windows.yml` builds the installer on a clean Windows
runner whenever a push to `main` changes `desktop/`, `static/` or the workflow
itself (or by hand: Actions → *Desktop app (Windows installer)* → Run workflow).
It runs `npm test` first, so a failing shell test blocks the release.

Each run replaces one rolling GitHub release, `desktop-latest`, with:

- `GerlynPLUS-Setup.exe` — stable name, so a link to it never changes
- `GerlynPLUS-Setup-<version>.exe` — the same file with its version

Web-only pushes (Python, templates) don't trigger a build; they don't need one.
The default server address comes from the repository variable
`GERLYN_SERVER_URL`.

A new build does **not** update machines that already installed the app (there
is no auto-updater): staff reinstall from the new file. Bump `version` in
`package.json` when you want that to be visible in the file name.

## Offering the installer to staff

The installer is ~120 MB; don't commit it or put it on Railway's disk. Use the
`desktop-latest` release above (or upload it anywhere with an https link) and
set `DESKTOP_APP_DOWNLOAD_URL` on the Railway service, e.g.
`https://github.com/<owner>/<repo>/releases/download/desktop-latest/GerlynPLUS-Setup.exe`.
Staff, branch managers and owners then see a **Download for Windows** card on
the Download page (customers don't).

That link is only as private as the repository: on a public repo anyone with it
can download the installer, and on a private repo staff without GitHub access
cannot. For a login-gated download use the private bucket below.

### Keeping the installer behind login (private bucket)

A public link works for anyone who has it. To let only signed-in staff fetch
the installer, put it in a **private** Backblaze bucket instead:

1. Create a new private bucket (not the backup bucket) and upload
   `GerlynPLUS-Setup-<version>.exe` to it.
2. Create an application key limited to that bucket with **read** access. The
   backup key is write-only and cannot be reused.
3. On the Railway service set `DESKTOP_APP_B2_BUCKET`, `DESKTOP_APP_B2_KEY_ID`,
   `DESKTOP_APP_B2_APPLICATION_KEY` and `DESKTOP_APP_B2_FILE` (the file name in
   the bucket). `DESKTOP_APP_B2_ENDPOINT` defaults to `B2_ENDPOINT`.
4. Leave `DESKTOP_APP_DOWNLOAD_URL` empty (if both are set, the private bucket wins).

The card then links to `/download/desktop/`. That page requires an Owner,
Branch Manager or Staff login (customers get 403, anonymous visitors the sign-in
page) and redirects to a Backblaze link that expires after 5 minutes. The file
goes browser to Backblaze; it never streams through Railway. To ship a new
version, upload it and change `DESKTOP_APP_B2_FILE` (download the new build
from the `desktop-latest` release or the workflow artifact first).

## Updates

The window shows the live server, so **every web deploy reaches desktop users
immediately** — nothing to reinstall. The shell itself (this folder) changes
rarely; when it does, bump `version` in `package.json`, rebuild and re-publish
the installer. (There is no auto-updater: it would need a signed release feed.)

## Tests

```powershell
npm test           # 51 unit tests: server-URL rules, navigation/permission
                   # policy, local-resource lookup, request handler, network
                   # pass-through, connectivity monitor
npm run test:e2e   # boots the real shell in Electron against a stand-in server
                   # that serves the repo's real sw.js and static/js/*.js
```

The end-to-end run covers: sign-in and redirects through the shell, an online
form post (Origin/Referer/cookies and a one-time flash message), service worker
and saved pages, bundled assets while the server is down, queueing a sale
offline, automatic sync, a lost response (applied once), a second account on the
same computer, launching with the server unreachable, blocked foreign
navigation, permissions, and the first-run setup screen.

It uses a **stand-in** for Django that mirrors the sync contract; the server's
own rules (permissions, scoping, stock, idempotency) are covered by
`python manage.py test offline_sync`.

> If Electron starts as plain Node ("`app` is undefined"), `ELECTRON_RUN_AS_NODE`
> is set in your shell (VS Code's terminal can do this). Clear it:
> `Remove-Item Env:ELECTRON_RUN_AS_NODE`.

## Layout

```
desktop/
  src/app.js         window, session, menu, IPC, wiring
  src/protocol.js    which requests are answered from the bundle
  src/network.js     faithful pass-through (redirects, cookies, bodies)
  src/resources.js   bundle lookup (path-safe, allow-listed)
  src/security.js    navigation / window.open / permission policy
  src/config.js      server-address rules and resolution
  src/monitor.js     is the server reachable (GET /offline/ping/)
  src/pages/         offline launch page, first-run setup page
  scripts/           bundle-assets.js, external.lock.json (pinned hashes)
  test/unit, test/e2e
```
