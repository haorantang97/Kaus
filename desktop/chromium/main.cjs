const { app, BrowserWindow, WebContentsView, ipcMain, session, dialog, Menu, shell } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const { randomUUID } = require('node:crypto');
const policy = require('./policy.cjs');

app.setName('Kaus Chromium Preview');
app.setPath('userData', path.join(app.getPath('appData'), 'KausChromiumPreview'));
app.enableSandbox();
let window, pendingDownloads = 0;
const editors = new Map(), popups = new Set();
const preferences = { sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true, allowRunningInsecureContent: false };
function restrict(contents, accepts) {
  for (const event of ['will-navigate', 'will-redirect']) contents.on(event, (e, url) => { if (!accepts(url)) e.preventDefault(); });
  contents.on('will-attach-webview', event => event.preventDefault());
}
function authorize(url, partition) {
  if (!policy.remote(url)) return;
  const popup = new BrowserWindow({ width: 980, height: 760, parent: window, title: 'ChatCut 授权', webPreferences: { ...preferences, partition } });
  popups.add(popup);
  restrict(popup.webContents, target => policy.remote(target) || policy.callback(target) || (policy.trusted(target) && new URL(target).pathname.startsWith('/workbench/')));
  popup.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  popup.on('closed', () => popups.delete(popup));
  void popup.loadURL(url).catch(() => {});
}
function ensureEditor(id) {
  if (editors.has(id)) return editors.get(id);
  if (editors.size >= 4) throw new Error('Editor limit');
  const partition = `persist:chatcut-${id}`, view = new WebContentsView({ webPreferences: { ...preferences, partition, backgroundThrottling: false } });
  const editor = { view, partition, url: null, status: {}, project: false };
  editors.set(id, editor); view.setVisible(false); window.contentView.addChildView(view);
  restrict(view.webContents, policy.remote);
  view.webContents.setWindowOpenHandler(({ url }) => { authorize(url, partition); return { action: 'deny' }; });
  view.webContents.on('render-process-gone', () => { editor.status = { error: '剪辑页面已中断，请确认导出状态后重新载入。' }; });
  const providerSession = session.fromPartition(partition);
  providerSession.setPermissionCheckHandler((_wc, name, origin) => name === 'fullscreen' && origin === 'https://app.chatcut.io');
  providerSession.setPermissionRequestHandler((wc, name, callback) => { let origin = ''; try { origin = new URL(wc.getURL()).origin; } catch {} callback(name === 'fullscreen' && origin === 'https://app.chatcut.io'); });
  providerSession.on('will-download', (event, item, contents) => {
    if (contents?.id !== view.webContents.id || !policy.remote(contents.getURL())) { event.preventDefault(); return; }
    const directory = path.join(app.getPath('downloads'), 'Kaus-ChatCut'); fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const name = policy.downloadName(item.getFilename()); let target = path.join(directory, name);
    if (fs.existsSync(target)) { const ext = path.extname(name); target = path.join(directory, path.basename(name, ext) + '-' + randomUUID().slice(0, 8) + ext); }
    item.setSavePath(target); pendingDownloads++;
    item.once('done', (_e, state) => { pendingDownloads--; editor.status = state === 'completed' ? { download: true, name: path.basename(target) } : { error: '导出文件未保存完成，请在 ChatCut 导出队列重新下载。' }; });
  });
  return editor;
}

app.whenReady().then(() => {
  window = new BrowserWindow({ width: 1600, height: 1000, minWidth: 1180, minHeight: 760, title: 'Kaus · 开发预览', webPreferences: { ...preferences, preload: path.join(__dirname, 'preload.cjs'), partition: 'kaus-preview-ui' } });
  restrict(window.webContents, policy.trusted);
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (policy.remote(url)) authorize(url, 'persist:chatcut-content-workbench');
    else { try { const u = new URL(url); if (['https:', 'http:'].includes(u.protocol) && !u.username && !u.password) void shell.openExternal(url); } catch {} }
    return { action: 'deny' };
  });
  window.on('close', event => {
    if (pendingDownloads) { event.preventDefault(); dialog.showMessageBoxSync(window, { message: '导出文件正在保存，请稍后关闭。', buttons: ['继续等待'] }); return; }
    if ([...editors.values()].some(e => e.project) && dialog.showMessageBoxSync(window, { message: '关闭开发预览？', detail: '请确认剪辑已保存，且没有正在渲染的导出。', buttons: ['取消', '关闭'], defaultId: 0, cancelId: 0 }) !== 1) event.preventDefault();
  });
  window.on('closed', () => { for (const popup of popups) popup.destroy(); for (const { view } of editors.values()) view.webContents.close(); editors.clear(); app.quit(); });
  ipcMain.handle('chatcut:surface', async (event, data) => {
    if (event.sender.id !== window.webContents.id || event.senderFrame !== window.webContents.mainFrame || !policy.trusted(event.senderFrame.url) || !data || !policy.moduleId(data.moduleId)) return { error: '剪辑权限无效。' };
    const id = data.moduleId;
    if (data.action === 'hide') { editors.get(id)?.view.setVisible(false); return { accepted: true }; }
    if (data.action === 'status') return editors.get(id)?.status || {};
    if (data.action === 'layout') {
      const size = window.getContentBounds(), bounds = policy.layout(data.bounds, size.width, size.height);
      if (!bounds || data.visible !== true) editors.get(id)?.view.setVisible(false);
      else { const editor = ensureEditor(id); for (const [other, value] of editors) if (other !== id) value.view.setVisible(false); editor.view.setBounds(bounds); editor.view.setVisible(true); }
    } else if (data.action === 'open' && policy.launch(data.url)) {
      const editor = ensureEditor(id);
      // Remounting a module resumes its running editor; only explicit project
      // launches replace it. Never persist ephemeral boot credentials.
      if (editor.url && new URL(data.url).pathname === '/zh') return { resumed: true };
      editor.project = new URL(data.url).pathname.includes('/editor/'); editor.url = data.url; editor.status = {};
      void editor.view.webContents.loadURL(data.url).catch(() => { editor.status = { error: 'ChatCut 页面未能载入，请重试连接。' }; });
    } else return { error: '剪辑请求无效。' };
    return { accepted: true };
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate([{ role: 'appMenu' }, { role: 'editMenu' }, { role: 'windowMenu' }]));
  void window.loadURL(policy.ORIGIN + '/workbench/content-workbench');
});
app.on('window-all-closed', () => app.quit());
