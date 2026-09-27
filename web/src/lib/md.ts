/* 极简 Markdown → HTML（从旧仪表盘移植）。先整段转义，再做标题/加粗/斜体/行内代码/
 * 列表/引用/链接/[[wikilink]]。
 *
 * batch37 第 1 件（外部评审 R1，P1）：**链接可以跳出 href 属性**。
 *   旧实现两处不够：① `esc()` 只处理 `& < >`，双引号原样进入 `href="…"`；
 *   ② 协议判断用的是"前缀 test 通过就把原字符串拼进去"，`[x](https://a" onmouseover=…)`
 *   于是变成一枚事件属性——助手正文与资料库正文都走 `dangerouslySetInnerHTML`，
 *   等于让转述来的外部内容以本页身份执行脚本。
 *
 *   现在的口径是三层，缺一不可：
 *     ① `esc()` 连引号一起转义（`"` `'`），文本位不可能再造出属性边界；
 *     ② 链接单独走 `safeUrl()`：先把 URL 从转义形态还原成**原文**（校验必须看原文），
 *        拒绝一切空白与控制字符（`java\nscript:` 这类靠换行拆词的写法在这里就没了）、
 *        显式挡掉 `javascript:` / `data:` / `vbscript:` / `file:`，再按白名单
 *        （`http(s):` / `mailto:` / `/` / `#`）放行；通过之后按"已有的百分号转义不再
 *        重复编码"的方式规范化，最后**按属性位再转义一次**才拼进 `href`；
 *     ③ 收尾把成品 HTML 里所有标签内的 `on*=` 属性剥掉（belt and braces）。这一层
 *        本不该有事可做——正文里的 `<` 早已是 `&lt;`，能匹配到的标签只有我们自己
 *        生成的那几种；它存在是为了将来谁在中间加了一段拼接时兜住。
 *
 * batch37 第 2 件（R8，P2）：代码块占位符原来是 `__FENCE_0__`，普通正文里出现同样的
 * 字面量就会被当成内部索引，对 `undefined` 调 `replace` 直接抛 TypeError。改成
 * **用户文本不可能产生**的记号：私有区字符 U+E000 / U+E001 包一个每次渲染随机的
 * nonce；进函数先把这两个私有区字符从输入里剔掉，于是碰撞在源头就不成立。索引仍然
 * 加一道保险：查不到就原样留下那段文本，不抛。
 */

/** 私有区包边：正文里出现这两个字符会在入口被剔掉，占位符因此不可伪造。 */
const FENCE_OPEN = "\uE000";
const FENCE_CLOSE = "\uE001";

/** 文本位转义。引号一起转——属性位复用同一函数，见 `escAttr`。 */
function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** 属性位转义。与 `esc` 同口径，单独命名是为了让调用处读得出"这是属性位"。 */
function escAttr(s: string): string {
  return esc(s);
}

/** 把 `esc()` 的结果还原成原始文本：URL 校验只能看原文，不能看转义形态。 */
function unesc(s: string): string {
  return s
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

/** 白名单：只有这四种开头算链接。其余（含 `example.com` 这类无协议写法）一律退回 `#`。 */
const ALLOWED_URL = /^(?:https?:|mailto:|\/|#)/i;
/** 显式黑名单。白名单已经挡住它们，这一条是写给读代码的人看的"绝不出现"。 */
const FORBIDDEN_SCHEME = /^(?:javascript|data|vbscript|file)\s*:/i;
/** 空白与控制字符：URL 里出现一个就整条不要（拆词绕过全靠它们）。 */
const CONTROL_OR_SPACE = /[\u0000-\u0020\u007f-\u009f\u2028\u2029]/;

/** 已经是合法百分号转义的片段原样保留，其余按 `encodeURI` 编码——不重复编码。 */
function encodeOnce(url: string): string {
  return url
    .split(/(%[0-9a-fA-F]{2})/)
    .map((part, index) => (index % 2 === 1 ? part : encodeURI(part)))
    .join("");
}

/** 通过校验返回可直接进属性的 URL；不通过返回 null（调用方退回 `#`）。 */
export function safeUrl(raw: string): string | null {
  const url = raw.trim();
  if (!url) return null;
  if (CONTROL_OR_SPACE.test(url)) return null;
  if (FORBIDDEN_SCHEME.test(url)) return null;
  if (!ALLOWED_URL.test(url)) return null;
  try { return encodeOnce(url); } catch { return null; }
}

/** 收尾：把标签内的 `on*=` 属性剥掉。正文里的 `<` 早是 `&lt;`，这里只会碰到自家标签。 */
function stripEventAttributes(html: string): string {
  return html.replace(/<[^>]*>/g, (tag) =>
    tag.replace(/\son[a-z0-9_:-]*\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]*)/gi, ""),
  );
}

export function renderMarkdown(src: string): string {
  if (!src) return "";
  /* 私有区包边字符先剔掉：占位符必须是用户造不出来的东西。 */
  const source = src.replace(/[\uE000\uE001]/g, "");
  const nonce = Math.random().toString(36).slice(2, 10);
  const fenceToken = (index: number) => `${FENCE_OPEN}f${nonce}:${index}${FENCE_CLOSE}`;
  const fences: string[] = [];
  let t = source.replace(/```(\w*)\n?([\s\S]*?)```/g, (_m, _lang, code: string) => {
    fences.push(code.replace(/\n$/, ""));
    return fenceToken(fences.length - 1);
  });
  t = esc(t);
  t = t
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>");
  t = t.replace(/^&gt; (.+)$/gm, "<blockquote>$1</blockquote>");
  t = t.replace(/(^- .+\n?)+/gm, (m) => "<ul>" + m.trim().split(/\n/).map((l) => `<li>${l.replace(/^- /, "")}</li>`).join("") + "</ul>");
  t = t.replace(/(^\d+\. .+\n?)+/gm, (m) => "<ol>" + m.trim().split(/\n/).map((l) => `<li>${l.replace(/^\d+\. /, "")}</li>`).join("") + "</ol>");
  t = t.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  t = t.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(?<![*\w])\*([^*\n]+)\*(?!\w)/g, "<em>$1</em>");
  t = t.replace(/\[\[([^\]]+)\]\]/g, '<span class="wikilink">$1</span>');
  t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_m, text: string, url: string) => {
    const href = safeUrl(unesc(url)) ?? "#";
    return `<a href="${escAttr(href)}" target="_blank" rel="noopener noreferrer">${text}</a>`;
  });
  const fencePattern = new RegExp(`${FENCE_OPEN}f${nonce}:(\\d+)${FENCE_CLOSE}`, "g");
  t = t.replace(fencePattern, (m, index: string) => {
    const code = fences[Number(index)];
    /* 认不出的索引就把原文留在那儿——渲染一段文本不该把整条消息炸掉（R8）。 */
    if (code === undefined) return m;
    return `<pre><code>${esc(code)}</code></pre>`;
  });
  t = t.split(/\n{2,}/).map((p) => {
    if (/^<(h\d|ul|ol|pre|blockquote)/.test(p.trim())) return p;
    return p.trim() ? `<p>${p.replace(/\n/g, "<br>")}</p>` : "";
  }).join("\n");
  return stripEventAttributes(t);
}
