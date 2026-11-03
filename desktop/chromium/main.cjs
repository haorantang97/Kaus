const { app, BrowserWindow, WebContentsView, ipcMain, session, dialog, Menu, shell, clipboard, ClipboardItem, nativeImage } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const { randomUUID } = require('node:crypto');
const policy = require('./policy.cjs');

app.setName('Kaus Chromium Preview');
app.setPath('userData', path.join(app.getPath('appData'), 'KausChromiumPreview'));
app.enableSandbox();
let window, pendingDownloads = 0;
const surfaces = new Map(), popups = new Set();
const preferences = { sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true, allowRunningInsecureContent: false };
// Host scripts run in their own world so pages cannot reach them; the DOM is shared.
const HOST_WORLD = 1001;
function restrict(contents, accepts) {
  for (const event of ['will-navigate', 'will-redirect']) contents.on(event, (e, url) => { if (!accepts(url)) e.preventDefault(); });
  contents.on('will-attach-webview', event => event.preventDefault());
}
function authorize(name, url, partition) {
  if (!policy.remote(name, url)) return;
  const popup = new BrowserWindow({ width: 980, height: 760, parent: window, title: `${policy.site(name).title} 授权`, webPreferences: { ...preferences, partition } });
  popups.add(popup);
  restrict(popup.webContents, target => policy.remote(name, target) || policy.callback(target) || (policy.trusted(target) && new URL(target).pathname.startsWith('/workbench/')));
  popup.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  popup.on('closed', () => popups.delete(popup));
  void popup.loadURL(url).catch(() => {});
}
function ensureSurface(id, name) {
  const key = `${id}:${name}`;
  if (surfaces.has(key)) return surfaces.get(key);
  if (surfaces.size >= 6) throw new Error('Surface limit');
  const config = policy.site(name);
  // ChatCut keeps its original partition so existing sign-ins survive.
  const partition = `persist:${name}-${id}`, view = new WebContentsView({ webPreferences: { ...preferences, partition, backgroundThrottling: false } });
  const surface = { id, name, view, partition, url: null, status: {}, project: false };
  surfaces.set(key, surface); view.setVisible(false); window.contentView.addChildView(view);
  restrict(view.webContents, url => policy.remote(name, url));
  view.webContents.setWindowOpenHandler(({ url }) => { authorize(name, url, partition); return { action: 'deny' }; });
  view.webContents.on('render-process-gone', () => { surface.status = { error: `${config.title} 页面已中断，请确认当前任务状态后重新载入。` }; });
  const providerSession = session.fromPartition(partition);
  providerSession.setPermissionCheckHandler((_wc, permission, origin) => permission === 'fullscreen' && !!config.fullscreen && origin === config.fullscreen);
  providerSession.setPermissionRequestHandler((wc, permission, callback) => { let origin = ''; try { origin = new URL(wc.getURL()).origin; } catch {} callback(permission === 'fullscreen' && !!config.fullscreen && origin === config.fullscreen); });
  providerSession.on('will-download', (event, item, contents) => {
    if (contents?.id !== view.webContents.id || !policy.remote(name, contents.getURL())) { event.preventDefault(); return; }
    save(item, config.downloads, `${config.title}-download`, (state, target) => { surface.status = state === 'completed' ? { download: true, name: path.basename(target) } : { error: `文件未保存完成，请在 ${config.title} 中重新下载。` }; });
  });
  return surface;
}
function save(item, folder, fallback, done) {
  const directory = path.join(app.getPath('downloads'), folder); fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const file = policy.downloadName(item.getFilename(), fallback); let target = path.join(directory, file);
  if (fs.existsSync(target)) { const ext = path.extname(file); target = path.join(directory, path.basename(file, ext) + '-' + randomUUID().slice(0, 8) + ext); }
  item.setSavePath(target); pendingDownloads++;
  item.once('done', (_e, state) => { pendingDownloads--; done(state, target); });
}
async function focusInput(contents, selectors, replace) {
  const source = `(${(selectors, replace) => {
    const usable = node => { const r = node.getBoundingClientRect(); return r.width > 0 && r.height > 0 && !node.disabled && !node.readOnly; };
    const editable = node => node && (node.isContentEditable || node.tagName === 'TEXTAREA' || (node.tagName === 'INPUT' && node.type === 'text'));
    let target = null;
    for (const selector of selectors) { target = [...document.querySelectorAll(selector)].find(usable); if (target) break; }
    if (!target && editable(document.activeElement) && usable(document.activeElement)) target = document.activeElement;
    if (!target) return false;
    target.focus();
    if (replace) { if (typeof target.select === 'function') target.select(); else document.execCommand('selectAll'); }
    return true;
  }})(${JSON.stringify(selectors)}, ${replace === true})`;
  contents.focus();
  return contents.executeJavaScriptInIsolatedWorld(HOST_WORLD, [{ code: source }], true);
}
async function savedClipboard() {
  const items = await clipboard.read().catch(() => []), saved = [];
  for (const item of items) {
    const entries = [];
    for (const type of item.types) if (type !== 'electron application/bookmark') { try { entries.push([type, await item.getType(type)]); } catch {} }
    if (entries.length) saved.push(new ClipboardItem(Object.fromEntries(entries)));
  }
  return saved;
}
async function fill(surface, data) {
  if (!surface?.url || surface.view.webContents.isLoading()) return { error: 'surface_not_ready' };
  if (data.text !== undefined) {
    const text = policy.fillText(surface.name, data.text);
    if (!text) return { error: 'invalid_request' };
    if (!await focusInput(surface.view.webContents, policy.site(surface.name).input.selectors, true)) return { error: 'input_not_found' };
    await surface.view.webContents.insertText(text);
  }
  if (data.image !== undefined) {
    const value = policy.fillImage(surface.name, data.image), image = value && nativeImage.createFromDataURL(value);
    if (!image || image.isEmpty()) return { error: 'invalid_request' };
    if (!await focusInput(surface.view.webContents, policy.site(surface.name).input.selectors, false)) return { error: 'input_not_found' };
    // Paste goes through the system clipboard; restore what the person had there.
    const saved = await savedClipboard();
    await clipboard.write([new ClipboardItem({ 'image/png': new Blob([image.toPNG()], { type: 'image/png' }) })]);
    surface.view.webContents.paste();
    setTimeout(() => { void (saved.length ? clipboard.write(saved) : Promise.resolve(clipboard.clear())).catch(() => {}); }, 800);
  }
  return { accepted: true };
}

// Side browser: tabs share one profile, apart from the Kaus UI and the site surfaces.
const browser = { tabs: new Map(), selected: null, bounds: null, seq: 0, pushing: false };
const BROWSER_PARTITION = 'persist:browser';
function browserState() {
  return { selected: browser.selected, tabs: [...browser.tabs.values()].map(({ id, view, url }) => {
    const wc = view.webContents;
    return { id, url: wc.getURL() || url, title: wc.getTitle(), loading: wc.isLoading(), canBack: wc.navigationHistory.canGoBack(), canForward: wc.navigationHistory.canGoForward() };
  }) };
}
function pushBrowser() {
  if (browser.pushing) return;
  browser.pushing = true;
  setImmediate(() => { browser.pushing = false; if (window && !window.isDestroyed()) window.webContents.send('kaus:browser-state', browserState()); });
}
function showBrowser() {
  for (const tab of browser.tabs.values()) {
    const shown = !!browser.bounds && tab.id === browser.selected;
    if (shown) tab.view.setBounds(browser.bounds);
    tab.view.setVisible(shown);
  }
}
function browserSession() {
  const s = session.fromPartition(BROWSER_PARTITION);
  if (s.kausReady) return s;
  s.kausReady = true;
  const allowed = permission => ['fullscreen', 'clipboard-sanitized-write'].includes(permission);
  s.setPermissionCheckHandler((_wc, permission) => allowed(permission));
  s.setPermissionRequestHandler((_wc, permission, callback) => callback(allowed(permission)));
  s.on('will-download', (_event, item) => save(item, 'Kaus-Browser', 'download', pushBrowser));
  return s;
}
function openTab(url) {
  if (browser.tabs.size >= 12) return null;
  browserSession();
  const id = `t${++browser.seq}`, view = new WebContentsView({ webPreferences: { ...preferences, partition: BROWSER_PARTITION } });
  const tab = { id, view, url };
  browser.tabs.set(id, tab); browser.selected = id;
  view.setVisible(false); window.contentView.addChildView(view);
  const wc = view.webContents;
  restrict(wc, policy.browsable);
  wc.setWindowOpenHandler(({ url: target }) => { if (policy.browsable(target) && openTab(target)) { showBrowser(); pushBrowser(); } return { action: 'deny' }; });
  for (const event of ['did-start-loading', 'did-stop-loading', 'did-navigate', 'did-navigate-in-page', 'page-title-updated', 'render-process-gone']) wc.on(event, pushBrowser);
  void wc.loadURL(url).catch(() => {});
  return tab;
}
function closeTab(id) {
  const tab = browser.tabs.get(id);
  if (!tab) return;
  const ids = [...browser.tabs.keys()], index = ids.indexOf(id);
  browser.tabs.delete(id); window.contentView.removeChildView(tab.view); tab.view.webContents.close();
  if (browser.selected === id) browser.selected = ids[index + 1] || ids[index - 1] || null;
}
async function browserFill(tab, value) {
  const wc = tab?.view.webContents;
  if (!wc || wc.isLoading()) return { error: 'surface_not_ready' };
  const { text, selectors, replace } = policy.browserText(value, wc.getURL());
  if (!text) return { error: 'invalid_request' };
  if (!await focusInput(wc, selectors, replace)) return { error: 'input_not_found' };
  await wc.insertText(text);
  return { accepted: true };
}
async function browserRequest(data) {
  const tab = browser.tabs.get(typeof data.tab === 'string' ? data.tab : browser.selected), wc = tab?.view.webContents;
  switch (data.action) {
    case 'state': return browserState();
    case 'layout': {
      const size = window.getContentBounds();
      browser.bounds = data.visible === true ? policy.layout(data.bounds, size.width, size.height) : null;
      if (browser.bounds) for (const surface of surfaces.values()) surface.view.setVisible(false);
      showBrowser(); return { accepted: true };
    }
    case 'open':
      if (!policy.browsable(data.url)) return { error: 'invalid_request' };
      if (!openTab(data.url)) return { error: 'tab_limit' };
      break;
    case 'navigate':
      if (!wc || !policy.browsable(data.url)) return { error: 'invalid_request' };
      void wc.loadURL(data.url).catch(() => {}); break;
    // A null tab clears the view so the address bar can start a new tab.
    case 'select': if (data.tab !== null && !tab) return { error: 'invalid_request' }; browser.selected = data.tab === null ? null : tab.id; break;
    case 'close': if (!tab) return { error: 'invalid_request' }; closeTab(tab.id); break;
    case 'back': if (wc?.navigationHistory.canGoBack()) wc.navigationHistory.goBack(); break;
    case 'forward': if (wc?.navigationHistory.canGoForward()) wc.navigationHistory.goForward(); break;
    case 'reload': wc?.reload(); break;
    case 'stop': wc?.stop(); break;
    // Filling places text where the page's input is; it never presses a key.
    case 'fill': return browserFill(tab, data.text);
    default: return { error: 'invalid_request' };
  }
  showBrowser(); pushBrowser();
  return { accepted: true };
}
function fromUi(event) { return event.sender.id === window.webContents.id && event.senderFrame === window.webContents.mainFrame && policy.trusted(event.senderFrame.url); }

app.whenReady().then(() => {
  window = new BrowserWindow({ width: 1600, height: 1000, minWidth: 1180, minHeight: 760, title: 'Kaus · 开发预览', webPreferences: { ...preferences, preload: path.join(__dirname, 'preload.cjs'), partition: 'kaus-preview-ui' } });
  restrict(window.webContents, policy.trusted);
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (policy.remote('chatcut', url)) authorize('chatcut', url, 'persist:chatcut-content-workbench');
    else { try { const u = new URL(url); if (['https:', 'http:'].includes(u.protocol) && !u.username && !u.password) void shell.openExternal(url); } catch {} }
    return { action: 'deny' };
  });
  window.on('close', event => {
    if (pendingDownloads) { event.preventDefault(); dialog.showMessageBoxSync(window, { message: '文件正在保存，请稍后关闭。', buttons: ['继续等待'] }); return; }
    if ([...surfaces.values()].some(s => s.project) && dialog.showMessageBoxSync(window, { message: '关闭开发预览？', detail: '请确认剪辑已保存，且没有正在渲染的导出。', buttons: ['取消', '关闭'], defaultId: 0, cancelId: 0 }) !== 1) event.preventDefault();
  });
  window.on('closed', () => { for (const popup of popups) popup.destroy(); for (const { view } of [...surfaces.values(), ...browser.tabs.values()]) view.webContents.close(); surfaces.clear(); browser.tabs.clear(); app.quit(); });
  ipcMain.handle('kaus:browser', async (event, data) => (fromUi(event) && data && typeof data.action === 'string' ? browserRequest(data) : { error: 'scope_denied' }));
  ipcMain.handle('kaus:surface', async (event, data) => {
    if (!fromUi(event) || !data || !policy.moduleId(data.moduleId) || !policy.site(data.site)) return { error: 'scope_denied' };
    const id = data.moduleId, name = data.site, key = `${id}:${name}`;
    if (data.action === 'hide') { surfaces.get(key)?.view.setVisible(false); return { accepted: true }; }
    if (data.action === 'status') return surfaces.get(key)?.status || {};
    if (data.action === 'fill') return policy.site(name).input ? fill(surfaces.get(key), data) : { error: 'invalid_request' };
    if (data.action === 'layout') {
      const size = window.getContentBounds(), bounds = policy.layout(data.bounds, size.width, size.height);
      if (!bounds || data.visible !== true) surfaces.get(key)?.view.setVisible(false);
      else {
        // A module may show several of its own sites side by side; other modules' surfaces yield.
        const surface = ensureSurface(id, name);
        for (const other of surfaces.values()) if (other.id !== id) other.view.setVisible(false);
        browser.bounds = null; showBrowser();
        surface.view.setBounds(bounds); surface.view.setVisible(true);
      }
    } else if (data.action === 'open' && policy.launch(name, data.url)) {
      const surface = ensureSurface(id, name), target = new URL(data.url), config = policy.site(name);
      // Remounting a module resumes its running page; only explicit project
      // launches replace it. Never persist ephemeral boot credentials.
      if (surface.url && config.home(target)) return { resumed: true };
      surface.project = config.project(target); surface.url = data.url; surface.status = {};
      void surface.view.webContents.loadURL(data.url).catch(() => { surface.status = { error: `${config.title} 页面未能载入，请重试连接。` }; });
    } else return { error: 'invalid_request' };
    return { accepted: true };
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate([{ role: 'appMenu' }, { role: 'editMenu' }, { role: 'windowMenu' }]));
  void window.loadURL(policy.ORIGIN + (/^\/(?!\/)/.test(process.env.KAUS_PREVIEW_PATH || '') ? process.env.KAUS_PREVIEW_PATH : '/workbench/content-workbench'));
});
app.on('window-all-closed', () => app.quit());
