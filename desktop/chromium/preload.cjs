const { contextBridge, ipcRenderer } = require('electron');
// Sandboxed preloads cannot require local files; policy.test.cjs keeps this list in step with SITES.
const SITES = Object.freeze(['chatcut', 'midjourney']);
// Only the trusted Kaus main frame gets this bridge. No module or remote preload.
contextBridge.exposeInMainWorld('kausSurfaceNative', Object.freeze({
  version: 2,
  sites: SITES,
  send: message => ipcRenderer.invoke('kaus:surface', message),
}));
// Native pickers and the isolated quota sign-in window (desktop.cjs).
contextBridge.exposeInMainWorld('kausDesktop', Object.freeze({
  version: 1,
  chooseDirectory: initialPath => ipcRenderer.invoke('kaus:desktop', { action: 'chooseDirectory', initialPath }),
  chooseExecutable: initialPath => ipcRenderer.invoke('kaus:desktop', { action: 'chooseExecutable', initialPath }),
  loginQuotaConnection: loginId => ipcRenderer.invoke('kaus:desktop', { action: 'loginQuotaConnection', loginId }),
}));
contextBridge.exposeInMainWorld('kausBrowserNative', Object.freeze({
  version: 1,
  send: message => ipcRenderer.invoke('kaus:browser', message),
  onState: listener => {
    const handler = (_event, state) => listener(state);
    ipcRenderer.on('kaus:browser-state', handler);
    return () => ipcRenderer.removeListener('kaus:browser-state', handler);
  },
}));
