// Pure helpers for the daily Kaus window (2026-11-24: the daily window moved from
// pywebview/WKWebView to this Chromium shell). No Electron imports: tested by daily.test.cjs.

/** The local Kaus backend. KAUS_ORIGIN may point the shell at another loopback port
 *  (the isolated ChatCut preview server uses 3187); anything else falls back. */
const DEFAULT_ORIGIN = 'http://127.0.0.1:8877';
function origin(value) {
  try {
    const u = new URL(value);
    if (u.protocol === 'http:' && u.hostname === '127.0.0.1' && /^\d{2,5}$/.test(u.port) && !u.username && !u.password && u.pathname === '/' && !u.search && !u.hash) return u.origin;
  } catch {}
  return DEFAULT_ORIGIN;
}

/** Start page path inside Kaus (KAUS_START_PATH), never another origin. */
function startPath(value) { return typeof value === 'string' && /^\/(?![/\\])[^\s]*$/.test(value) ? value : '/'; }

// Menu text follows the macOS language, like the interface's default「跟随系统」.
const MENU_TEXT = Object.freeze({
  zh: Object.freeze({ settings: '设置', file: '文件', newConversation: '新对话', newProject: '新建项目', edit: '编辑', view: '显示', window: '窗口', zoomReset: '实际大小', zoomIn: '放大', zoomOut: '缩小', fullscreen: '进入全屏幕' }),
  en: Object.freeze({ settings: 'Settings', file: 'File', newConversation: 'New Conversation', newProject: 'New Project', edit: 'Edit', view: 'View', window: 'Window', zoomReset: 'Actual Size', zoomIn: 'Zoom In', zoomOut: 'Zoom Out', fullscreen: 'Enter Full Screen' }),
});
function menuLanguage(preferred) { return Array.isArray(preferred) && typeof preferred[0] === 'string' && preferred[0].toLowerCase().startsWith('zh') ? 'zh' : 'en'; }
function menuText(preferred) { return MENU_TEXT[menuLanguage(preferred)]; }

const ZOOM_STEPS = Object.freeze([0.75, 0.85, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0]);
/** The next zoom step from `current`; direction 0 resets to 1. */
function nextZoom(current, direction) {
  if (direction === 0) return 1;
  if (direction > 0) return ZOOM_STEPS.find(step => step > current + 1e-6) ?? ZOOM_STEPS.at(-1);
  return [...ZOOM_STEPS].reverse().find(step => step < current - 1e-6) ?? ZOOM_STEPS[0];
}

/** Page layout rectangles are in CSS pixels; native views are placed in window pixels. */
function scaleBounds(bounds, zoom) {
  if (!bounds || typeof bounds !== 'object' || !(zoom > 0)) return bounds;
  const out = {};
  for (const key of Object.keys(bounds)) out[key] = typeof bounds[key] === 'number' ? bounds[key] * zoom : bounds[key];
  return out;
}

// Quota sign-in (StepFun). Mirrors kernel/drivers/explicit_quota/web_login.py capture_session:
// only this fresh window's Oasis-Token cookie on the selected platform; the backend validates it.
const STEPFUN = Object.freeze({ ai: 'https://platform.stepfun.ai', com: 'https://platform.stepfun.com' });
const TOKEN_LIMIT = 16384;
const LOGIN_ID = /^[A-Za-z0-9_-]{32,64}$/;
function claims(token) {
  if (typeof token !== 'string' || token.length > TOKEN_LIMIT || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token)) return null;
  try { const value = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8')); return value && typeof value === 'object' && !Array.isArray(value) ? value : null; } catch { return null; }
}
function identity(value) { const text = typeof value === 'number' && Number.isInteger(value) ? String(value) : value; return typeof text === 'string' && text.length <= 256 && /^[A-Za-z0-9._:-]+$/.test(text); }
function stepfunLoginUrl(site) { return Object.hasOwn(STEPFUN, site) ? `${STEPFUN[site]}/step-plan` : null; }
function stepfunTokens(site, pageUrl, cookies) {
  if (!Object.hasOwn(STEPFUN, site) || !Array.isArray(cookies)) return null;
  try {
    const page = new URL(pageUrl), expected = new URL(STEPFUN[site]);
    if (page.protocol !== 'https:' || page.hostname !== expected.hostname || (page.port && page.port !== '443') || page.username || page.password) return null;
  } catch { return null; }
  const domains = new Set([`platform.stepfun.${site}`, `stepfun.${site}`]);
  for (const cookie of cookies) {
    if (!cookie || cookie.name !== 'Oasis-Token' || !domains.has(String(cookie.domain || '').replace(/^\./, '')) || !cookie.secure || (cookie.path || '/') !== '/') continue;
    const raw = cookie.value;
    if (typeof raw !== 'string' || raw.length > TOKEN_LIMIT * 2 + 3) continue;
    const parts = raw.split('...');
    if (parts.length !== 2) continue;
    // Wait until the platform has activated this login and the refresh grant names its device;
    // the backend still validates the session with the platform before saving it.
    const access = claims(parts[0]), grant = claims(parts[1]);
    if (!access || !grant || access.activated !== true || !identity(access.oasis_id) || !identity(grant.device_id)) continue;
    if (grant.oasis_id !== undefined && String(grant.oasis_id) !== String(access.oasis_id)) continue;
    return { access: parts[0], refresh: parts[1] };
  }
  return null;
}

/** Where moved data goes: the chosen folder itself when it is empty (or new), otherwise a "Kaus"
 *  folder inside it. Never the current folder, inside it, or around it. */
function moveTarget(current, chosen, chosenIsEmpty) {
  const norm = value => typeof value === 'string' && value.startsWith('/') ? value.replace(/\/+$/, '') || '/' : null;
  const from = norm(current), base = norm(chosen);
  if (!from || !base) return { error: 'invalid_location' };
  const target = chosenIsEmpty ? base : `${base === '/' ? '' : base}/Kaus`;
  if (target === from) return { error: 'same_location' };
  if (target.startsWith(`${from}/`) || from.startsWith(`${target}/`)) return { error: 'nested_location' };
  return { target };
}

module.exports = { moveTarget, DEFAULT_ORIGIN, origin, startPath, MENU_TEXT, menuLanguage, menuText, ZOOM_STEPS, nextZoom, scaleBounds, STEPFUN, LOGIN_ID, stepfunLoginUrl, stepfunTokens };
