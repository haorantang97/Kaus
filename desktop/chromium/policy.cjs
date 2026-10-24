const ORIGIN = 'http://127.0.0.1:3187';
function parse(value) { try { return typeof value === 'string' && value.length <= 16384 ? new URL(value) : null; } catch { return null; } }
function trusted(value) { const u = parse(value); return !!u && u.origin === ORIGIN && !u.username && !u.password; }
function launch(value) { const u = parse(value); return !!u && u.origin === 'https://app.chatcut.io' && !u.username && !u.password && (u.pathname === '/zh' || /^\/(?:zh\/|es\/)?editor\/[A-Za-z0-9_-]{1,160}\/?$/.test(u.pathname)); }
function remote(value) { const u = parse(value); return !!u && !u.username && !u.password && ['https://app.chatcut.io', 'https://api.chatcut.io', 'https://accounts.google.com'].includes(u.origin); }
function callback(value) { const u = parse(value); return trusted(value) && /^\/api\/workbench\/callback\/[a-z][a-z0-9-]{0,63}$/.test(u.pathname); }
function moduleId(value) { return typeof value === 'string' && /^[a-z][a-z0-9-]{0,63}$/.test(value); }
function layout(value, width, height) {
  if (!value || typeof value !== 'object' || Object.keys(value).sort().join() !== 'height,width,x,y') return null;
  if (Object.values(value).some(n => typeof n !== 'number' || !Number.isFinite(n))) return null;
  const { x, y, width: w, height: h } = value;
  if (x < 0 || y < 0 || w < 1 || h < 1 || x + w > width + 1 || y + h > height + 1) return null;
  return { x: Math.round(x), y: Math.round(y), width: Math.min(Math.round(w), width - Math.round(x)), height: Math.min(Math.round(h), height - Math.round(y)) };
}
function downloadName(value) { return String(value || 'ChatCut-export').replaceAll('\\', '/').split('/').pop().replace(/[\x00-\x1f:]/g, '').replace(/^[. ]+|[. ]+$/g, '').slice(0, 180) || 'ChatCut-export'; }
module.exports = { ORIGIN, trusted, launch, remote, callback, moduleId, layout, downloadName };
