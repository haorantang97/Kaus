#!/usr/bin/env node
/* 组件边界的静态检查（component-boundaries.md §5.4 的前端版，对应内核已有的
   kernel/tests/test_public_type_purity.py）。跑法：npm run lint
 *
 * 三条规则：
 *   ① 引擎名零出现：页面与卡片组件里不得出现 hermes / codex / claude 字样，
 *      也不得出现 backendId === "…" 这类相等判断。引擎差异只能通过能力值表达
 *      （边界①）。白名单：fixture 目录（那是引擎真发的数据）与 web/src/backends/<id>/
 *      （将来的引擎专属面板）。
 *   ② detail 层不进对话页：卡片子树不得 import 能力的 detail 层，也不得出现
 *      .note / .verification / capabilities.detail（边界③ / AD-71）。
 *   ③ 界面文案零硬编码英文（AD-96 / 批次十一第 4 件）：`pages/` 与 `components/`
 *      里的 JSX 文本节点不得出现 ≥2 个连续的拉丁单词——那种字符串一定是没进词典
 *      的旧文案，切到英文界面时不会变，切到中文界面时就是"语言突变"。文案一律走
 *      `t()`。豁免：测试文件。属性（`className=…` / `placeholder="…"`）不是文本
 *      节点，本来就不在检查范围里——`aria-*` 之类同理，它们该用 `t()` 但由人工把关。
 *
 * 检查是文本级的，故意做得笨：它要在 CI 里三秒跑完，并且违规时给出行号。
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const WEB = fileURLToPath(new URL("..", import.meta.url));
const SRC = join(WEB, "src");

/** 扫描范围：页面与组件（对应边界①的原文）。 */
const SCANNED_DIRS = [join(SRC, "pages"), join(SRC, "components")];

/** 白名单：引擎专属面板与 fixture。 */
const ALLOWED = [
  join(SRC, "backends") + sep,
  join(SRC, "lib", "backend-display.ts"),
  `${sep}__fixtures__${sep}`,
  `${sep}__snapshots__${sep}`,
];

/* Phase 2 之前就存在的组件：它们是冻结域或待拆分的旧界面，去品牌化是另一项任务
   （批次七第 ② 项）。这份清单只减不增——**新建的文件一律受检**，这正是这条检查
   要防的事：新写的页面/卡片不许再长出引擎名分支。清完一个就从这里删一行。 */
const LEGACY = [
  "Conversation.tsx",
  "ConfigOverlay.tsx",
  "KanbanOverlay.tsx",
  "ProfileDrawer.tsx",
  "TerminalLab.tsx",
  "UpdateHealthAlert.tsx",
  "WarehouseOverlay.tsx",
  "ui.tsx",
].map((name) => join(SRC, "components", name));

/** 只有这棵子树属于「对话页」，规则②盯的就是它。 */
const CONVERSATION_SUBTREE = [join(SRC, "components", "cards") + sep, join(SRC, "pages") + sep];

const ENGINE_NAMES = /\b(hermes|codex|claude)\b/i;
const BACKEND_ID_BRANCH = /backendId\s*[=!]==?\s*["'`]/;
const DETAIL_LAYER = /capabilities\s*\.\s*detail|\bfetchBackendCapabilityDetail\b|\.\s*verification\b|\.\s*note\b/;

/* 规则③的三块：
   - TEXT_NODE：`>…<` / `}…{` 之间那段，即 JSX 的文本节点（每行单看，故意做得笨）。
     开头那个 `>` 前面不能是 `=`（否则 `=>` 会把箭头函数体当成文本）。
   - CODEISH：带 `=` / 引号 / 反引号 / 分号的一律当代码，不是文案（属性串、模板串）。
   - LATIN_PAIR：两个挨着的拉丁单词。前面不能是 `.` 或 `/`，免得把 `SKILL.md URL`
     这类路径片段当成句子。 */
const TEXT_NODE = /(^|[^=!<>-])[>}]([^<>{}]+?)(?=[<{])/g;
const CODEISH = /[="`;]/;
const LATIN_PAIR = /(?<![./A-Za-z])[A-Za-z][A-Za-z'’]*(?:[-/][A-Za-z]+)*[ \t]+[A-Za-z][A-Za-z'’]*/;
const isComment = (line) => {
  const trimmed = line.trim();
  return trimmed.startsWith("//") || trimmed.startsWith("*") || trimmed.startsWith("/*");
};
const hardcodedLatin = (line) => {
  if (isComment(line)) return null;
  for (const match of line.matchAll(TEXT_NODE)) {
    const text = match[2];
    if (CODEISH.test(text)) continue;
    if (LATIN_PAIR.test(text)) return text.trim();
  }
  return null;
};

function walk(dir) {
  let out = [];
  let entries;
  try {
    entries = readdirSync(dir);
  } catch {
    return out; // 目录还不存在（例如 pages/ 由并行分支建）——不算失败。
  }
  for (const entry of entries) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out = out.concat(walk(full));
    else if (/\.(ts|tsx)$/.test(full)) out.push(full);
  }
  return out;
}

// 测试文件里的引擎名是测试数据（和 fixture 同性质），不是页面逻辑。
const isTestFile = (file) => /\.test\.tsx?$/.test(file);
const allowed = (file) =>
  ALLOWED.some((prefix) => file.includes(prefix)) || LEGACY.includes(file) || isTestFile(file);
const inConversationSubtree = (file) => CONVERSATION_SUBTREE.some((prefix) => file.startsWith(prefix));

const problems = [];
for (const dir of SCANNED_DIRS) {
  for (const file of walk(dir)) {
    const skipEngineRules = allowed(file);
    // 规则③豁免：测试文件与 fixture（那是测试数据，不是界面文案）。
    const skipTextRule =
      isTestFile(file) || file.includes(`${sep}__fixtures__${sep}`) || file.includes(`${sep}__snapshots__${sep}`);
    const lines = readFileSync(file, "utf8").split("\n");
    lines.forEach((line, index) => {
      const where = `${relative(WEB, file)}:${index + 1}`;
      if (!skipEngineRules && ENGINE_NAMES.test(line)) {
        problems.push(`${where} 出现引擎名：${line.trim()}`);
      }
      if (!skipEngineRules && BACKEND_ID_BRANCH.test(line)) {
        problems.push(`${where} 按 backendId 分支：${line.trim()}`);
      }
      if (!skipEngineRules && inConversationSubtree(file) && DETAIL_LAYER.test(line)) {
        problems.push(`${where} 对话页子树读了能力的 detail 层：${line.trim()}`);
      }
      const latin = skipTextRule ? null : hardcodedLatin(line);
      if (latin !== null) {
        problems.push(`${where} 硬编码英文文案（请走 t()）：${latin}`);
      }
    });
  }
}

if (problems.length > 0) {
  console.error("组件边界检查未通过（docs/frontend/component-boundaries.md §2）：");
  for (const problem of problems) console.error("  - " + problem);
  process.exit(1);
}
console.log("组件边界检查通过：引擎名零出现，对话页子树只读能力的 ui 层，界面文案零硬编码英文。");
