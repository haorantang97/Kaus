const { contextBridge, ipcRenderer } = require('electron');
// Sandboxed preloads cannot require local files; policy.test.cjs keeps this list in step with SITES.
const SITES = Object.freeze(['chatcut', 'midjourney']);
// Only the trusted Kaus main frame gets this bridge. No module or remote preload.
contextBridge.exposeInMainWorld('kausSurfaceNative', Object.freeze({
  version: 2,
  sites: SITES,
  send: message => ipcRenderer.invoke('kaus:surface', message),
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
