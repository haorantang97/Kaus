/* 界面偏好：一律走 localStorage，**不进 URL、不进全局 store**
 * （component-boundaries §4.2 的第三类状态）。localStorage 在隐私模式/测试环境
 * 可能不可用，所有读写都吞掉异常并回退到默认值——偏好丢了不该让界面崩。
 */

export const SIDEBAR_PINNED_KEY = "kaus-sidebar-pinned-v1";
export const SIDEBAR_GROUPS_KEY = "kaus-sidebar-collapsed-groups-v1";
export const NAV_MORE_KEY = "kaus-nav-more-open-v1";
export const DEFAULT_DESTINATION_KEY = "kaus-default-destination-v1";
export const DRAFT_TEXT_KEY = "kaus-new-conversation-draft-v1";
/* `kaus-shell-route-migrated-v1` 在批次十一取消：旧 localStorage 外壳状态不再
   迁移成路由（带旧状态的用户就是回概览），键留在浏览器里但没人读了。 */

/** 「默认去向」的出厂值：根项目 X（IA 叫法表：slug `default` = 界面上的 X）。 */
export const DEFAULT_DESTINATION_FALLBACK = "project:default";

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* 存不下就算了：偏好不是数据 */
  }
}

export function readSidebarPinned(): boolean {
  return read(SIDEBAR_PINNED_KEY) === "1";
}

export function writeSidebarPinned(pinned: boolean): void {
  write(SIDEBAR_PINNED_KEY, pinned ? "1" : "0");
}

export function readCollapsedGroups(): string[] {
  const raw = read(SIDEBAR_GROUPS_KEY);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as unknown;
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

export function writeCollapsedGroups(ids: string[]): void {
  write(SIDEBAR_GROUPS_KEY, JSON.stringify(ids));
}

/* batch40（DESIGN ★L）：导航里「更多」那一组开着还是折着。
   **出厂是折着**——那五个旧面板不是每天要用的东西，常显只是把三个真入口挤窄。
   记 localStorage（同这一文件的其余偏好：读写都吞异常，存不下就回默认）。 */
export function readNavMoreOpen(): boolean {
  return read(NAV_MORE_KEY) === "1";
}

export function writeNavMoreOpen(open: boolean): void {
  write(NAV_MORE_KEY, open ? "1" : "0");
}

export function readDefaultDestination(): string {
  return read(DEFAULT_DESTINATION_KEY) || DEFAULT_DESTINATION_FALLBACK;
}

export function writeDefaultDestination(projectId: string): void {
  write(DEFAULT_DESTINATION_KEY, projectId);
}

/* 草稿正文按「本次页面会话」保留：sessionStorage，刷新即丢（★定稿 C）。 */
export function readDraftText(): string {
  try {
    return window.sessionStorage.getItem(DRAFT_TEXT_KEY) ?? "";
  } catch {
    return "";
  }
}

export function writeDraftText(text: string): void {
  try {
    if (text) window.sessionStorage.setItem(DRAFT_TEXT_KEY, text);
    else window.sessionStorage.removeItem(DRAFT_TEXT_KEY);
  } catch {
    /* ignore */
  }
}

/** 相对时间：侧栏一行只放得下四五个字。 */
export function relativeTime(iso: string, locale: "zh" | "en", now: number = Date.now()): string {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return "";
  const tag = locale === "zh" ? "zh-CN" : "en";
  const format = new Intl.RelativeTimeFormat(tag, { numeric: "auto", style: "narrow" });
  const seconds = Math.max(0, Math.round((now - at) / 1000));
  if (seconds < 60) return format.format(0, "second");
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return format.format(-minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (hours < 24) return format.format(-hours, "hour");
  const days = Math.round(hours / 24);
  if (days < 30) return format.format(-days, "day");
  return new Date(at).toLocaleDateString(tag);
}
