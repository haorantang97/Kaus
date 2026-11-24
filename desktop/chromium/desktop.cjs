// Daily-window services that used to live in the pywebview shell (desktop/app.py,
// workspace_bridge.py, quota_login.py, app_menu.py; retired 2026-11-24): native pickers,
// the isolated quota sign-in window, the backend check and the menu bar.
const { app, BrowserWindow, Menu, dialog, ipcMain, session } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { randomUUID } = require('node:crypto');
const daily = require('./daily.cjs');

const BACKEND_SERVICE = 'io.github.haorantang97.kaus.backend';
const LOGIN_SECONDS = 600;

async function healthy(origin, timeout = 1500) {
  try { return (await fetch(`${origin}/api/health`, { signal: AbortSignal.timeout(timeout) })).ok; } catch { return false; }
}
/** The backend is a launchd service; if it does not answer, kick it once and wait. */
async function ensureBackend(origin) {
  if (await healthy(origin)) return true;
  const uid = typeof process.getuid === 'function' ? process.getuid() : null;
  if (uid !== null) await new Promise(resolve => execFile('launchctl', ['kickstart', `gui/${uid}/${BACKEND_SERVICE}`], () => resolve()));
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) { if (await healthy(origin)) return true; await new Promise(r => setTimeout(r, 400)); }
  return false;
}
function backendDownPage(zh) {
  const title = zh ? 'Kaus 后台未启动' : 'The Kaus backend is not running';
  const body = zh ? '请重新打开 Kaus；仍无法启动时，在终端执行：' : 'Reopen Kaus. If it still does not start, run in Terminal:';
  const html = `<!doctype html><meta charset="utf-8"><title>Kaus</title><body style="font:14px -apple-system,sans-serif;padding:3em;color:#242428;background:#fcfcfb"><h2>${title}</h2><p>${body}</p><pre>launchctl kickstart -k gui/$(id -u)/${BACKEND_SERVICE}</pre></body>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

function installDesktop({ getWindow, origin, isUi, preferences }) {
  let busy = false; // one native dialog or sign-in window at a time
  const guarded = async (event, run) => {
    if (!isUi(event)) throw new Error('scope_denied');
    if (busy) return null;
    busy = true;
    try { return await run(); } finally { busy = false; }
  };
  const startDir = (value, file) => {
    if (typeof value !== 'string' || !path.isAbsolute(value)) return app.getPath('home');
    try { const stat = fs.statSync(value); return stat.isDirectory() ? value : file ? path.dirname(value) : app.getPath('home'); } catch { return app.getPath('home'); }
  };

  ipcMain.handle('kaus:desktop', async (event, data) => {
    const action = data && typeof data.action === 'string' ? data.action : '';
    if (action === 'chooseDirectory') return guarded(event, async () => {
      const result = await dialog.showOpenDialog(getWindow(), { defaultPath: startDir(data.initialPath, false), properties: ['openDirectory', 'createDirectory'] });
      if (result.canceled || !result.filePaths[0]) return null;
      const chosen = fs.realpathSync(result.filePaths[0]);
      if (!fs.statSync(chosen).isDirectory()) throw new Error('The selected directory is unavailable');
      return chosen;
    });
    if (action === 'chooseExecutable') return guarded(event, async () => {
      const result = await dialog.showOpenDialog(getWindow(), { defaultPath: startDir(data.initialPath, true), properties: ['openFile'] });
      if (result.canceled || !result.filePaths[0]) return null;
      const chosen = path.resolve(result.filePaths[0]);
      try { fs.accessSync(chosen, fs.constants.X_OK); } catch { throw new Error('The selected file is not executable'); }
      if (!fs.statSync(chosen).isFile()) throw new Error('The selected file is not executable');
      return chosen;
    });
    if (action === 'loginQuotaConnection') {
      if (!isUi(event)) return { status: 'failed', reason: 'login_failed' };
      if (busy) return { status: 'failed', reason: 'login_in_progress' };
      busy = true;
      try { return await quotaLogin(String(data.loginId || '')); } finally { busy = false; }
    }
    return { error: 'invalid_request' };
  });

  /** Only the fresh sign-in window's credentials cross the local API boundary. */
  async function quotaLogin(loginId) {
    if (!daily.LOGIN_ID.test(loginId)) return { status: 'failed', reason: 'login_failed' };
    let token = null;
    const request = async (part, payload) => {
      const response = await fetch(`${origin}/api/${part}`, {
        method: payload === undefined ? 'GET' : 'POST',
        headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(payload === undefined ? {} : { 'Content-Type': 'application/json' }) },
        body: payload === undefined ? undefined : JSON.stringify(payload),
        signal: AbortSignal.timeout(part.endsWith('/complete') ? 45000 : 4000),
      });
      return response.json();
    };
    const base = `quota-connections/login/${loginId}`;
    try {
      token = (await request('session-auth/bootstrap')).token;
      const login = await request(`${base}/claim`, {});
      const url = login?.provider === 'stepfun-plan' ? daily.stepfunLoginUrl(login.config?.site) : null;
      if (!url) return { status: 'failed', reason: 'login_failed' };
      const secrets = await stepfunWindow(login.config.site, url, async () => {
        try { return !['waiting', 'verifying'].includes((await request(base)).status); } catch { return true; }
      });
      return secrets ? request(`${base}/complete`, { secrets }) : request(`${base}/cancel`, {});
    } catch {
      if (token) { try { await request(`${base}/complete`, { reason: 'login_failed' }); } catch {} }
      return { status: 'failed', reason: 'login_failed' };
    } finally { token = null; }
  }

  /** A new in-memory profile per sign-in: no Kaus, browser or other profile cookies. */
  function stepfunWindow(site, url, cancelled) {
    return new Promise(resolve => {
      const partition = `stepfun-login-${randomUUID()}`, ses = session.fromPartition(partition, { cache: false });
      const zh = daily.menuLanguage(app.getPreferredSystemLanguages()) === 'zh';
      const login = new BrowserWindow({ width: 980, height: 740, minWidth: 620, minHeight: 480, parent: getWindow(),
        title: zh ? `登录 StepFun · ${site === 'com' ? '中国站' : '国际站'}` : `Sign in to StepFun · ${site === 'com' ? 'China' : 'International'}`,
        webPreferences: { ...preferences, partition } });
      const wc = login.webContents;
      for (const name of ['will-navigate', 'will-redirect']) wc.on(name, (e, target) => { if (!/^https:/i.test(target)) e.preventDefault(); });
      wc.setWindowOpenHandler(({ url: target }) => { if (/^https:/i.test(target)) void wc.loadURL(target).catch(() => {}); return { action: 'deny' }; });
      const started = Date.now();
      let done = false, polling = false, ticks = 0;
      const finish = value => {
        if (done) return;
        done = true; clearInterval(timer);
        void ses.clearStorageData().catch(() => {});
        if (!login.isDestroyed()) login.destroy();
        resolve(value);
      };
      const timer = setInterval(async () => {
        if (done || polling) return;
        if (Date.now() - started > LOGIN_SECONDS * 1000) return finish(null);
        polling = true;
        try {
          if (++ticks % 2 === 0 && await cancelled()) return finish(null);
          const tokens = daily.stepfunTokens(site, wc.getURL(), await ses.cookies.get({}));
          if (tokens) finish(tokens);
        } catch {} finally { polling = false; }
      }, 750);
      login.on('closed', () => finish(null));
      void login.loadURL(url).catch(() => {});
    });
  }
}

/** File / Edit / View / Window, with the page commands of the web app (window.kausCommand). */
function buildMenu(getWindow) {
  const text = daily.menuText(app.getPreferredSystemLanguages());
  const command = name => { const w = getWindow(); if (w && !w.isDestroyed()) void w.webContents.executeJavaScript(`window.kausCommand && window.kausCommand(${JSON.stringify(name)})`).catch(() => {}); };
  const zoom = direction => { const w = getWindow(); if (!w || w.isDestroyed()) return; w.webContents.setZoomFactor(daily.nextZoom(w.webContents.getZoomFactor(), direction)); };
  return Menu.buildFromTemplate([
    { role: 'appMenu', submenu: [
      { role: 'about' }, { type: 'separator' },
      { label: text.settings, accelerator: 'CmdOrCtrl+,', click: () => command('settings') },
      { type: 'separator' }, { role: 'services' }, { type: 'separator' },
      { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' }, { type: 'separator' }, { role: 'quit' },
    ] },
    { label: text.file, submenu: [
      { label: text.newConversation, accelerator: 'CmdOrCtrl+N', click: () => command('newConversation') },
      { label: text.newProject, accelerator: 'Shift+CmdOrCtrl+N', click: () => command('newProject') },
    ] },
    { role: 'editMenu', label: text.edit },
    { label: text.view, submenu: [
      { label: text.zoomReset, accelerator: 'CmdOrCtrl+0', click: () => zoom(0) },
      { label: text.zoomIn, accelerator: 'CmdOrCtrl+=', click: () => zoom(1) },
      { label: text.zoomOut, accelerator: 'CmdOrCtrl+-', click: () => zoom(-1) },
      { type: 'separator' }, { role: 'togglefullscreen', label: text.fullscreen },
    ] },
    { role: 'windowMenu', label: text.window },
  ]);
}

module.exports = { BACKEND_SERVICE, ensureBackend, backendDownPage, installDesktop, buildMenu };
