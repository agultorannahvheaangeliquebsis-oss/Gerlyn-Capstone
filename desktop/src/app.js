'use strict';
/**
 * GerlynPLUS desktop shell.
 *
 * A thin Electron wrapper around the live Django site. The UI, sign-in, roles,
 * permissions, branch scoping and the offline queue are all the web app's own;
 * this process adds only:
 *   - a window pinned to one server origin;
 *   - local copies of the site's static files (so pages render offline);
 *   - an offline launch page;
 *   - a hardened browsing surface (no foreign navigation, no extra
 *     permissions, no node in pages).
 */
const fs = require('fs');
const path = require('path');
const {
  app, BrowserWindow, Menu, dialog, ipcMain, net, screen, session, shell,
} = require('electron');

const config = require('./config');
const security = require('./security');
const { createLogger } = require('./log');
const { LocalResources, LAUNCH_PATH } = require('./resources');
const { createHandler } = require('./protocol');
const { ConnectivityMonitor } = require('./monitor');
const { createNetworkFetch } = require('./network');

const PARTITION = 'persist:gerlynplus';
const APP_ID = 'com.gerlynplus.desktop';
const SETUP_PAGE = path.join(__dirname, 'pages', 'setup.html');

function resourcesDir() {
  // Packaged: <install>/resources. Development: ../ (the desktop/ folder).
  return app.isPackaged ? process.resourcesPath : path.join(__dirname, '..');
}

function run(options = {}) {
  if (!app.requestSingleInstanceLock()) {
    // A second copy would open a second view of the same offline queue.
    app.quit();
    return null;
  }

  app.setAppUserModelId(APP_ID);

  const ctx = {
    win: null,
    setupWin: null,
    server: null,       // {origin, source, locked}
    ses: null,
    monitor: null,
    log: null,
    lastNetworkError: '',
    ready: null,
    options,
  };

  app.on('second-instance', () => {
    const win = ctx.win || ctx.setupWin;
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });

  app.on('window-all-closed', () => app.quit());

  // Every web contents the app ever creates gets the same rules, including
  // windows opened by pages (window.open) and print previews.
  app.on('web-contents-created', (_event, contents) => hardenContents(ctx, contents));

  ctx.ready = app.whenReady().then(() => start(ctx));
  return ctx;
}

// ── start-up ────────────────────────────────────────────────────────────────

async function start(ctx) {
  const userData = app.getPath('userData');
  ctx.log = createLogger(path.join(userData, 'logs'));
  ctx.log.info(`GerlynPLUS Desktop ${app.getVersion()} starting (electron ${process.versions.electron})`);

  ctx.ses = session.fromPartition(PARTITION);
  ctx.server = config.resolveServer({ userDataDir: userData, resourcesDir: resourcesDir() });

  // Identify ourselves to the server (Sessions page, access logs) without
  // pretending to be a different browser.
  const defaultUa = ctx.ses.getUserAgent()
    .replace(/\s*Electron\/\S+/i, '')
    .replace(/\s*gerlynplus-desktop\/\S+/i, '');
  ctx.ses.setUserAgent(`${defaultUa} GerlynPLUS-Desktop/${app.getVersion()}`);

  installPermissionRules(ctx);
  installRequestHandler(ctx);
  installIpc(ctx);
  Menu.setApplicationMenu(buildMenu(ctx));

  ctx.monitor = new ConnectivityMonitor({
    getOrigin: () => (ctx.server ? ctx.server.origin : ''),
    fetchNetwork: (url, init) => ctx.ses.fetch(url, init),
    onChange: (online) => ctx.log.info(`connectivity: ${online ? 'online' : 'offline'}`),
  });

  if (ctx.server) {
    ctx.monitor.start();
    openMainWindow(ctx);
  } else {
    openSetupWindow(ctx);
  }
}

// ── networking ──────────────────────────────────────────────────────────────

function installRequestHandler(ctx) {
  const bundleDir = path.join(resourcesDir(), 'bundled');
  const resources = new LocalResources({ bundleDir, pagesDir: path.join(__dirname, 'pages') });
  if (!resources.hasStaticBundle()) {
    ctx.log.warn(`no static bundle at ${bundleDir}; run "npm run bundle". Pages will use the network and the saved-page cache only.`);
  }
  ctx.resources = resources;

  const handler = createHandler({
    getOrigin: () => (ctx.server ? ctx.server.origin : ''),
    resources,
    isOnline: () => (ctx.monitor ? ctx.monitor.isOnline() : null),
    fetchNetwork: createNetworkFetch({ net, session: ctx.ses }),
    onNetworkError: (error) => {
      ctx.lastNetworkError = String((error && (error.cause && error.cause.message || error.message)) || error);
    },
  });

  // 'https' is the production scheme. 'http' is only ever used against a
  // localhost / private-network server (config.normalizeServerUrl).
  ctx.ses.protocol.handle('https', handler);
  ctx.ses.protocol.handle('http', handler);
}

function installPermissionRules(ctx) {
  const originOf = () => (ctx.server ? ctx.server.origin : '');
  ctx.ses.setPermissionRequestHandler((_wc, permission, callback, details) => {
    let allowed = security.allowPermission(permission, details.requestingUrl || '', originOf());
    // The barcode scanner needs the camera and nothing else.
    if (allowed && permission === 'media') {
      const types = details.mediaTypes || [];
      allowed = types.length > 0 && types.every((t) => t === 'video');
    }
    callback(allowed);
  });
  ctx.ses.setPermissionCheckHandler((_wc, permission, requestingOrigin) =>
    security.allowPermission(permission, requestingOrigin || '', originOf()));
}

// ── windows ─────────────────────────────────────────────────────────────────

function hardenContents(ctx, contents) {
  contents.on('will-attach-webview', (event) => event.preventDefault());

  contents.on('will-navigate', (event, url) => {
    if (isLocalAppPage(url)) return;
    if (ctx.server && security.allowNavigation(url, ctx.server.origin)) return;
    event.preventDefault();
    if (security.allowExternal(url)) openExternal(ctx, url);
  });

  contents.on('will-redirect', (event, url) => {
    if (ctx.server && security.allowNavigation(url, ctx.server.origin)) return;
    event.preventDefault();
  });

  contents.setWindowOpenHandler(({ url }) => {
    const action = security.windowOpenAction(url, ctx.server ? ctx.server.origin : '');
    if (action === 'allow') return { action: 'allow' };
    if (action === 'external') openExternal(ctx, url);
    return { action: 'deny' };
  });
}

// Callers have already passed the URL through security.allowExternal().
function openExternal(ctx, url) {
  const open = ctx.options.openExternal || ((u) => shell.openExternal(u));
  return open(url);
}

function isLocalAppPage(url) {
  return url.startsWith('file://') && url.toLowerCase().endsWith('/pages/setup.html');
}

function windowStateFile() {
  return path.join(app.getPath('userData'), 'window-state.json');
}

function loadWindowState() {
  const fallback = { width: 1366, height: 820 };
  try {
    const s = JSON.parse(fs.readFileSync(windowStateFile(), 'utf8'));
    const visible = screen.getAllDisplays().some((d) => {
      const b = d.workArea;
      return s.x >= b.x - 20 && s.y >= b.y - 20 && s.x < b.x + b.width && s.y < b.y + b.height;
    });
    if (Number.isFinite(s.width) && Number.isFinite(s.height)) {
      return { ...fallback, width: s.width, height: s.height, ...(visible ? { x: s.x, y: s.y } : {}), maximized: !!s.maximized };
    }
  } catch (e) { /* first run */ }
  return fallback;
}

function saveWindowState(win) {
  try {
    const maximized = win.isMaximized();
    const b = maximized ? win.getNormalBounds() : win.getBounds();
    fs.writeFileSync(windowStateFile(), JSON.stringify({ ...b, maximized }));
  } catch (e) { /* not worth failing a close over */ }
}

function iconPath() {
  const candidates = [
    path.join(__dirname, '..', 'build', 'icon.png'),
    path.join(resourcesDir(), 'icon.png'),
  ];
  return candidates.find((p) => fs.existsSync(p));
}

function webPreferences(preload) {
  return {
    preload,
    partition: PARTITION,
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    webSecurity: true,
    allowRunningInsecureContent: false,
    // The offline queue syncs from timers. A minimised window must keep doing
    // that, otherwise Pending Sync would stall until it is brought back.
    backgroundThrottling: false,
    devTools: !app.isPackaged || process.env.GERLYN_DESKTOP_DEVTOOLS === '1',
  };
}

function openMainWindow(ctx) {
  const state = loadWindowState();
  const win = new BrowserWindow({
    width: state.width,
    height: state.height,
    x: state.x,
    y: state.y,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: 'GerlynPLUS',
    backgroundColor: '#ffffff',
    icon: iconPath(),
    autoHideMenuBar: false,
    webPreferences: webPreferences(path.join(__dirname, 'preload-app.js')),
  });
  ctx.win = win;
  if (state.maximized) win.maximize();

  win.once('ready-to-show', () => { if (ctx.options.showWindows !== false) win.show(); });
  win.on('close', () => saveWindowState(win));
  win.on('closed', () => { if (ctx.win === win) ctx.win = null; });

  win.webContents.on('did-fail-load', (_event, code, description, validatedUrl, isMainFrame) => {
    if (!isMainFrame || code === -3) return; // -3: superseded by another navigation
    ctx.log.warn(`load failed (${code} ${description}) for ${redact(validatedUrl)}`);
    showLaunchPage(ctx, win, validatedUrl, description);
  });

  // A detail from an earlier, unrelated request must not be blamed for the
  // next failure.
  win.webContents.on('did-start-navigation', (_event, _url, _inPlace, isMainFrame) => {
    if (isMainFrame) ctx.lastNetworkError = '';
  });

  win.webContents.on('render-process-gone', (_e, details) => {
    ctx.log.error(`renderer gone: ${details.reason}`);
    if (details.reason !== 'clean-exit' && !win.isDestroyed()) win.reload();
  });

  loadStart(ctx, win);
  return win;
}

function loadStart(ctx, win) {
  win.loadURL(`${ctx.server.origin}/`).catch(() => { /* did-fail-load shows the launch page */ });
}

function showLaunchPage(ctx, win, failedUrl, description) {
  if (win.isDestroyed() || !ctx.server) return;
  let failedPath = '/';
  try {
    const u = new URL(failedUrl);
    if (u.origin === ctx.server.origin) {
      if (u.pathname.startsWith('/__gerlyn_desktop__/')) {
        // The launch page itself failed (bundle missing?). Do not loop.
        ctx.log.error('launch page failed to load');
        return;
      }
      failedPath = u.pathname + u.search;
    }
  } catch (e) { /* keep '/' */ }
  const detail = ctx.lastNetworkError || description || '';
  const query = new URLSearchParams({ from: failedPath, detail });
  win.loadURL(`${ctx.server.origin}${LAUNCH_PATH}?${query}`).catch(() => {});
}

function openSetupWindow(ctx) {
  if (ctx.setupWin && !ctx.setupWin.isDestroyed()) {
    ctx.setupWin.focus();
    return;
  }
  const win = new BrowserWindow({
    width: 560,
    height: 520,
    resizable: false,
    minimizable: true,
    show: false,
    title: 'GerlynPLUS — Server',
    backgroundColor: '#f4f6f5',
    icon: iconPath(),
    autoHideMenuBar: true,
    webPreferences: {
      ...webPreferences(path.join(__dirname, 'preload-setup.js')),
      partition: undefined, // setup talks to nothing but the main process
    },
  });
  ctx.setupWin = win;
  win.setMenuBarVisibility(false);
  win.once('ready-to-show', () => { if (ctx.options.showWindows !== false) win.show(); });
  win.on('closed', () => {
    if (ctx.setupWin === win) ctx.setupWin = null;
    // Closing setup with nothing configured has nothing left to show.
    if (!ctx.server && !ctx.win) app.quit();
  });
  win.loadFile(SETUP_PAGE);
}

// ── IPC (setup page only) ───────────────────────────────────────────────────

function fromSetupPage(ctx, event) {
  const frame = event.senderFrame;
  const url = frame ? frame.url : '';
  return !!ctx.setupWin && event.sender === ctx.setupWin.webContents && isLocalAppPage(url);
}

function installIpc(ctx) {
  ipcMain.handle('desktop:server-info', (event) => {
    if (!fromSetupPage(ctx, event)) return {};
    return ctx.server ? { origin: ctx.server.origin, locked: ctx.server.locked } : {};
  });

  ipcMain.handle('desktop:cancel-setup', (event) => {
    if (!fromSetupPage(ctx, event)) return;
    if (ctx.setupWin) ctx.setupWin.close();
  });

  ipcMain.handle('desktop:save-server', async (event, payload) => {
    if (!fromSetupPage(ctx, event)) return { ok: false, error: 'Not allowed.' };
    if (ctx.server && ctx.server.locked) {
      return { ok: false, error: 'The server address is set by the environment and cannot be changed.' };
    }
    const parsed = config.normalizeServerUrl(payload && payload.url);
    if (!parsed.ok) return { ok: false, error: parsed.error };

    if (!(payload && payload.force)) {
      const reachable = await probe(ctx, parsed.origin);
      if (!reachable) {
        return {
          ok: false,
          unreachable: true,
          error: 'Could not reach that server right now. Check the address and your connection, '
            + 'or save it anyway if you are sure it is right.',
        };
      }
    }

    config.writeSettings(app.getPath('userData'), { serverUrl: parsed.origin });
    const changed = !ctx.server || ctx.server.origin !== parsed.origin;
    ctx.server = { origin: parsed.origin, source: 'settings', locked: false };
    ctx.log.info(`server set to ${parsed.origin}`);
    ctx.monitor.start();
    ctx.monitor.probe();

    // Let this reply reach the setup page before its window goes away.
    const setupWin = ctx.setupWin;
    if (setupWin) setImmediate(() => { if (!setupWin.isDestroyed()) setupWin.close(); });
    if (ctx.win && !ctx.win.isDestroyed()) {
      if (changed) loadStart(ctx, ctx.win);
      ctx.win.focus();
    } else {
      openMainWindow(ctx);
    }
    return { ok: true };
  });
}

async function probe(ctx, origin) {
  try {
    const res = await ctx.ses.fetch(`${origin}/offline/ping/?t=${Date.now()}`, {
      bypassCustomProtocolHandlers: true,
      cache: 'no-store',
      signal: AbortSignal.timeout(5000),
      headers: { Accept: 'application/json' },
    });
    const body = res.ok ? await res.json() : null;
    return !!(body && body.ok === true);
  } catch (e) {
    return false;
  }
}

// ── menu ────────────────────────────────────────────────────────────────────

function buildMenu(ctx) {
  const open = (pathName) => () => {
    if (ctx.win && ctx.server) ctx.win.loadURL(`${ctx.server.origin}${pathName}`).catch(() => {});
  };
  const isDev = !app.isPackaged || process.env.GERLYN_DESKTOP_DEVTOOLS === '1';

  const template = [
    {
      label: 'File',
      submenu: [
        { label: 'Home', accelerator: 'CmdOrCtrl+Shift+H', click: open('/') },
        { label: 'Open in Browser', click: () => {
          const url = ctx.win && ctx.win.webContents.getURL();
          const target = url && ctx.server && security.sameOrigin(url, ctx.server.origin)
            ? url : (ctx.server && ctx.server.origin);
          if (target && security.allowExternal(target)) openExternal(ctx, target);
        } },
        { type: 'separator' },
        { label: 'Server Address…', click: () => {
          if (ctx.server && ctx.server.locked) {
            dialog.showMessageBox({
              type: 'info',
              message: 'The server address is set by the GERLYN_SERVER_URL environment variable.',
              detail: ctx.server.origin,
            });
            return;
          }
          openSetupWindow(ctx);
        } },
        { type: 'separator' },
        { role: 'quit', label: 'Exit' },
      ],
    },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
        ...(isDev ? [{ type: 'separator' }, { role: 'toggleDevTools' }] : []),
      ],
    },
    {
      label: 'Go',
      submenu: [
        { label: 'Back', accelerator: 'Alt+Left', click: () => {
          const wc = ctx.win && ctx.win.webContents;
          if (wc && wc.canGoBack()) wc.goBack();
        } },
        { label: 'Forward', accelerator: 'Alt+Right', click: () => {
          const wc = ctx.win && ctx.win.webContents;
          if (wc && wc.canGoForward()) wc.goForward();
        } },
      ],
    },
    {
      label: 'Help',
      submenu: [
        { label: 'About GerlynPLUS Desktop', click: () => {
          dialog.showMessageBox(ctx.win || undefined, {
            type: 'info',
            title: 'About GerlynPLUS Desktop',
            message: `GerlynPLUS Desktop ${app.getVersion()}`,
            detail: [
              `Server: ${ctx.server ? ctx.server.origin : 'not set'}`,
              `Connection: ${ctx.monitor && ctx.monitor.isOnline() === false ? 'offline' : 'online'}`,
              `Electron ${process.versions.electron} · Chromium ${process.versions.chrome}`,
              '',
              'Changes you make while offline are saved on this computer and sent to the server automatically when the connection returns.',
            ].join('\n'),
          });
        } },
        { label: 'Open Log Folder', click: () => shell.openPath(path.join(app.getPath('userData'), 'logs')) },
      ],
    },
  ];
  return Menu.buildFromTemplate(template);
}

/** Paths are fine to log; query strings may carry search terms or tokens. */
function redact(url) {
  try {
    const u = new URL(url);
    return `${u.origin}${u.pathname}`;
  } catch (e) {
    return '(unparseable url)';
  }
}

module.exports = { run, PARTITION };
