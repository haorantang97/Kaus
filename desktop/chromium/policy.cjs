const ORIGIN = 'http://127.0.0.1:3187';
function parse(value) { try { return typeof value === 'string' && value.length <= 16384 ? new URL(value) : null; } catch { return null; } }
function plain(u) { return !!u && !u.username && !u.password; }
// Each site is a fixed surface, not a browser: launch pages, navigation origins,
// downloads and optional input filling are declared here and nowhere else.
const SITES = Object.freeze({
  chatcut: Object.freeze({
    title: 'ChatCut', downloads: 'Kaus-ChatCut', fullscreen: 'https://app.chatcut.io',
    origins: ['https://app.chatcut.io', 'https://api.chatcut.io', 'https://accounts.google.com'],
    launch: u => u.origin === 'https://app.chatcut.io' && (u.pathname === '/zh' || /^\/(?:zh\/|es\/)?editor\/[A-Za-z0-9_-]{1,160}\/?$/.test(u.pathname)),
    home: u => u.pathname === '/zh',
    project: u => u.pathname.includes('/editor/'),
    input: null,
  }),
  midjourney: Object.freeze({
    title: 'Midjourney', downloads: 'Kaus-Midjourney', fullscreen: null,
    origins: ['https://www.midjourney.com', 'https://midjourney.com', 'https://accounts.google.com', 'https://discord.com'],
    launch: u => u.origin === 'https://www.midjourney.com' && /^\/(?:home|imagine|explore|create|organize|archive|edit|jobs\/[A-Za-z0-9-]{1,64})?\/?$/.test(u.pathname),
    home: u => !u.pathname.startsWith('/jobs/'),
    project: () => false,
    // Filling never submits: the person presses generate on the site.
    input: Object.freeze({ selectors: ['textarea#desktop_input_bar', 'textarea[placeholder*="imagine" i]', 'textarea'], singleLine: true, maxLength: 6000, images: true }),
  }),
});
function site(name) { return typeof name === 'string' && Object.hasOwn(SITES, name) ? SITES[name] : null; }
function trusted(value) { const u = parse(value); return plain(u) && u.origin === ORIGIN; }
function launch(name, value) { const s = site(name), u = parse(value); return !!s && plain(u) && s.launch(u); }
function remote(name, value) { const s = site(name), u = parse(value); return !!s && plain(u) && s.origins.includes(u.origin); }
function callback(value) { const u = parse(value); return trusted(value) && /^\/api\/workbench\/callback\/[a-z][a-z0-9-]{0,63}$/.test(u.pathname); }
function moduleId(value) { return typeof value === 'string' && /^[a-z][a-z0-9-]{0,63}$/.test(value); }
function layout(value, width, height) {
  if (!value || typeof value !== 'object' || Object.keys(value).sort().join() !== 'height,width,x,y') return null;
  if (Object.values(value).some(n => typeof n !== 'number' || !Number.isFinite(n))) return null;
  const { x, y, width: w, height: h } = value;
  if (x < 0 || y < 0 || w < 1 || h < 1 || x + w > width + 1 || y + h > height + 1) return null;
  return { x: Math.round(x), y: Math.round(y), width: Math.min(Math.round(w), width - Math.round(x)), height: Math.min(Math.round(h), height - Math.round(y)) };
}
function downloadName(value, fallback = 'ChatCut-export') { return String(value || fallback).replaceAll('\\', '/').split('/').pop().replace(/[\x00-\x1f:]/g, '').replace(/^[. ]+|[. ]+$/g, '').slice(0, 180) || fallback; }
function cleanText(input, value) {
  if (!input || typeof value !== 'string') return null;
  let text = value.replace(/\r\n?/g, '\n').replace(/[\x00-\x08\x0b-\x1f\x7f]/g, '');
  if (input.singleLine) text = text.replace(/\s*\n\s*/g, ' ');
  text = text.trim();
  return text && text.length <= input.maxLength ? text : null;
}
function fillText(name, value) { return cleanText(site(name)?.input, value); }
function fillImage(name, value) {
  if (!site(name)?.input?.images || typeof value !== 'string' || value.length > 12_000_000) return null;
  return /^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/.test(value) ? value : null;
}
// The side browser goes anywhere on the web except this host itself; pages get no host bridge.
const BROWSER_INPUT = Object.freeze({ selectors: [], singleLine: false, maxLength: 20000, images: false });
function browsable(value) {
  const u = parse(value);
  if (!plain(u) || !['http:', 'https:'].includes(u.protocol)) return false;
  const local = ['127.0.0.1', 'localhost', '[::1]', '0.0.0.0'].includes(u.hostname) || u.hostname.endsWith('.localhost');
  return !(local && ['3187', '8877'].includes(u.port));
}
// A site profile for the page, matched by its main origin, refines where text goes.
function profile(value) {
  const u = parse(value);
  if (!u) return null;
  for (const [name, s] of Object.entries(SITES)) if (s.input && s.origins[0] === u.origin) return { name, input: s.input };
  return null;
}
function browserText(value, url) { const p = profile(url); return { text: cleanText(p?.input || BROWSER_INPUT, value), selectors: p?.input.selectors || [], replace: !!p }; }
module.exports = { ORIGIN, SITES, site, trusted, launch, remote, callback, moduleId, layout, downloadName, fillText, fillImage, browsable, profile, browserText };
