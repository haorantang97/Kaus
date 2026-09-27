# 上下文索引层（Context Index Layer）架构设计

> 状态：设计稿 v1（2026-07-21），未实施。
> 目标：补齐 Hermes 组织树的**语义索引层**，让 36 个 agent 从"信息孤岛 + 两条没人知道的隧道"变成"一张有地图的上下文网络"。
> 灵感来源：Jeff Su Cowork 体系中根 CLAUDE.md 的 Routing Map（索引即灵魂）；但 Hermes 是多 agent 架构，需要把"单大脑 + 共享文件系统"的免费共享，重造为"索引 + 横向管道"的受治理共享。

---

## 1. 问题定义

**现状**：上下文只有一条垂直向下的流（宪法/config 继承）。横向管道已存在但是"死毛细血管"：

| 已有管道 | 能力 | 为什么没被用 |
|---|---|---|
| `hermes_vault_mcp` | 任意 agent 读写全局 llm wiki | agent 不知道库里有什么、什么该进库 |
| `hermes_delegate_mcp.call_agent` | 以对方人格+记忆+技能真实跑一次拿判断 | agent 不知道其他 agent 存在、各自懂什么 |

**缺口**：没有任何一份「谁知道什么 / 什么在哪 / 什么任务找谁」的地图被注入到任何 agent 的上下文。`hierarchy.json` 只有父子结构，无语义。

**设计原则**：
1. **索引是中枢，不是又一份文档**——所有跨域行为（直调/查库/派发）必须先过索引。
2. **零 token 放大**——索引本体绝不注入 SOUL（×36 广播不可接受），宪法里只放一句指针，按需加载。
3. **记忆保持私有**——不做 MEMORY.md 互读。跨域共享只走两条受治理的路：蒸馏进 vault（沉淀知识）、call_agent（即时判断）。
4. **生成不手写**——索引由确定性扫描 + 可选 LLM 蒸馏自动生成，agent 增删改自动刷新，遵循现有 sync-tick 模式。
5. **复用一切已有机制**——宪法广播、mtime 脏检查、原子写、selfcheck、steward 分身。不新建平行系统。

---

## 2. 总架构图

```
                        ┌──────────────────────────────────────┐
                        │  constitution.md（宪法·根）           │
                        │  新增一条: [rule:context-index]       │
                        │  "跨域任务先读 INDEX，再选管道"        │
                        └────────────────┬─────────────────────┘
                                         │ ⓵ 垂直广播（已有:_constitution_sync_tick）
              ┌──────────────────────────┼──────────────────────────┐
              ▼                          ▼                          ▼
      SOUL.md (X/root)           SOUL.md (media)            SOUL.md (trading) … ×36
              │                          │                          │
              │        ⓶ 按需读取（新增行为，由宪法指针触发）          │
              └──────────────┬───────────┴──────────────────────────┘
                             ▼
              ┌──────────────────────────────────┐
              │  ~/.hermes/INDEX.md  ★新增中枢    │
              │  ├ Agent 名录(领域/我知道/找我时机) │
              │  ├ Vault 地图(知识库目录语义)      │
              │  ├ 任务路由表(任务类型→agent)      │
              │  └ 状态标记(active/dormant/shell) │
              └───────▲──────────────────────────┘
                      │ ⓷ 自动生成+刷新（新增:_index_sync_tick，
                      │    仿 _constitution_sync_tick / mtime 脏检查）
        ┌─────────────┴─────────────────────────────┐
        │ 数据源(全部已存在):                          │
        │  hierarchy.json / labels.json              │
        │  各 profile SOUL.md persona 区              │
        │  各 profile memories/MEMORY.md（仅摘要行数/主题,│
        │    不进原文）                                │
        │  skills externals / state.db 会话活跃度      │
        └────────────────────────────────────────────┘

   查完索引后，agent 在两条已有横向管道中选路：

   任意 agent ── ⓸ call_agent(target, prompt) ──► 目标 agent（带本人记忆/技能真实跑）
        │              （已有: hermes_delegate_mcp）
        │
        └──── ⓹ vault_read / vault_write ──► llm wiki 全局知识库
                       （已有: hermes_vault_mcp）

   会话结束后的回流（第二期）：

   session-audit skill ── ⓺ 蒸馏 ──► 全局事实→根 MEMORY / 领域事实→本 agent MEMORY
                                     / 沉淀知识→vault ──► 触发 ⓷ 索引刷新
```

五条流的属性：⓵⓸⓹ 已存在；⓶⓷ 本设计新增；⓺ 二期（session-audit skill）。

---

## 3. INDEX.md 数据模型

生成文件位于 `~/.hermes/INDEX.md`，结构（含受管块标记，仿 SOUL.md 的宪法块）：

```markdown
<!-- ⚑ HERMES INDEX · 由 dashboard 自动生成，勿手改本区块（底部有手写覆盖区） -->
# Hermes 上下文索引
> 生成时间: 2026-07-21T12:00:00 | 树版本: <hierarchy.json mtime hash>

## Agent 名录
### X（根 · default）
- 领域: 组织治理、意图分发
- 找我时机: 需要跨域协调、新建 agent、改组织结构
- 状态: active

### media（媒体）
- 领域: 内容创作 / 公众号 / 视频脚本
- 我知道: <蒸馏摘要，一行，来自 persona+记忆主题>
- 找我时机: 需要产出对外内容、改写成品文案
- 工具面: skills[social-media, creative] · mcp[vault]
- 状态: active（最近会话 07-19）

### fashion（时尚）
- 状态: shell（persona 为默认模板且记忆为空——索引明确标注，
        防止路由到空壳 agent）
…（×36，shell/dormant 排在末尾）

## Vault 地图
- /交易/ : 交易复盘、策略笔记
- /内容/ : 选题库、已发布存档
…（来自 vault_list 顶层目录 + 手写语义注释）

## 任务路由表
| 任务特征 | 首选 | 备选 | 管道 |
|---|---|---|---|
| 产出对外内容 | media | personal-website | call_agent |
| 交易/行情判断 | trading | — | call_agent |
| 抓取/自动化 | web-automation | scrapling | call_agent |
| 查已有知识 | — | — | vault_read |
| 仪表盘故障 | dashboard-ops | dashboard-project | call_agent |
<!-- ⚑ END INDEX -->

## 手写覆盖区（此区之下不会被生成器触碰）
（用户可固定某些路由规则或补充语义，生成器保留）
```

**字段来源与生成方式**：

| 字段 | 来源 | 方式 |
|---|---|---|
| id / 花名 | profiles/ + labels.json | 确定性 |
| 树位置 | hierarchy.json | 确定性 |
| 领域 / 找我时机 | SOUL.md persona 区首段 | 确定性截取；persona=默认模板 → 标记 shell |
| 我知道 | memories/MEMORY.md | 一期: 条目数+关键词；二期: LLM 蒸馏一行 |
| 工具面 | skills externals + config mcp_servers | 确定性 |
| 状态 | state.db 最近会话时间 | active(<30d) / dormant / shell |
| Vault 地图 | vault_list 顶层 | 确定性 + 手写覆盖区注释 |
| 路由表 | 一期手写覆盖区起步；二期由名录自动导出 | 混合 |

**安全红线**：生成器绝不读取 `.env` / auth / credentials；记忆只取主题词不引原文；正则过滤疑似密钥模式后才写入 INDEX（复用 redact 逻辑思路）。

---

## 4. 生成与刷新机制

仿照 `_constitution_sync_tick` 的成熟模式：

```
server.py 新增:
  _build_index()          # 纯函数: 扫描数据源 → 渲染受管块 → 保留手写覆盖区 → 原子写
  _index_sync_tick()      # 慢 tick(建议 10min) + mtime 脏检查:
                          #   监视 hierarchy.json / labels.json / 各 SOUL.md /
                          #   各 memories/MEMORY.md / skills 目录 mtime
  生命周期钩子直接调用:      # 建/删/改名 agent、宪法保存、技能 allocate 后立即重建
  POST /api/index/rebuild # 手动触发（dashboard 按钮 / MCP 工具）
```

- **写入**：走 `_write_config` 同款原子写 + 锁；绝不裸 write_text。
- **selfcheck.py 增补一条断言**：INDEX 存在、可解析、生成时间 < 24h、受管块完整。
- **二期 LLM 蒸馏**："我知道"一行的语义摘要，由 **steward 分身**（共享根记忆，天然图书管理员角色）以 `hermes oneshot` 批量生成，只在脏 agent 上增量跑，避免每次全量烧 token。

---

## 5. 分发与接入

1. **宪法新增一条**（走 dashboard 宪法面板保存，自动广播全树）：

```markdown
### [rule:context-index]
遇到超出你领域、或需要其他领域知识/判断的任务：
1. 先读 ~/.hermes/INDEX.md（read_file 绝对路径）；
2. 按路由表选择管道：需要"对方的判断"→ call_agent(target, …)；
   需要"已沉淀的知识"→ vault_read；
3. 不查索引不得臆测其他 agent 的存在或能力；
   目标 agent 状态为 shell/dormant 时如实告知用户，不得强行路由。
```

2. **INDEX 本体按需加载**：agent 用文件工具读绝对路径（根 .env 注入机制已证明 profile 进程可访问根路径）。可选增强：在 `hermes_dashboard_mcp` 加 `index_read` / `index_rebuild` 两个工具，统一走 API、方便未来加 ACL。
3. **token 账**：宪法只增 ~8 行 ×36；INDEX（预计 100–150 行）只在跨域时刻被读一次。对比"把名录塞进每个 SOUL"方案节省一个数量级。

---

## 6. 复杂任务逻辑路径（核心图示）

示例任务（对根 X 说）：**"把我上个月的交易复盘写成一篇公众号文章并发布"**

```
 用户
  │  ①下达任务
  ▼
┌─────────────────────────────────────────────────────────────────┐
│ X (root)  SOUL=宪法(含[rule:context-index])+根记忆+USER画像       │
└──┬──────────────────────────────────────────────────────────────┘
   │ ②识别为跨域复合任务 → 读 ~/.hermes/INDEX.md
   ▼
┌──────────────── INDEX 命中 ────────────────┐
│ 交易判断→trading(call_agent)               │
│ 复盘原始笔记→vault:/交易/(vault_read)       │
│ 成文→media(call_agent)                    │
│ 排版发布→web-auto-production(call_agent)   │
│ (若有环节未命中→走"未命中路径"见下)          │
└──────────────┬────────────────────────────┘
   │ ③分解为有依赖的步骤（可选:写入 kanban 子任务卡）
   │
   │ ④第一步·拿判断
   ├──► call_agent(trading, "上月复盘要点+核心结论")
   │      └ trading 以本人记忆/技能真实跑 → 返回"它本人的判断"
   │
   │ ⑤第二步·拿沉淀
   ├──► vault_read("/交易/2026-06-复盘.md")   ← 原始素材
   │
   │ ⑥第三步·成文
   ├──► call_agent(media, "以下要点+素材成文" + 指针:voice-principles)
   │      └ media 的宪法链已含"输出前读 voice-principles"
   │        → 产出符合本人语气的文章草稿
   │
   │ ⑦第四步·发布
   ├──► call_agent(web-auto-production, "排版+发布到公众号")
   │      └ 失败/风控 → 按宪法如实报告 blocker，绝不伪造结果
   │
   │ ⑧汇总结果+产物路径 → 回复用户
   ▼
 用户确认
   │
   │ ⑨会话收尾（二期）: /session-audit
   ▼
┌────────────── 蒸馏三分法 ──────────────┐
│ 全局事实(如新偏好) → 根 MEMORY.md      │
│ 领域事实(如发布参数) → 该 agent MEMORY │
│ 沉淀知识(如文章存档) → vault           │
└──────────────┬────────────────────────┘
               │ ⑩mtime 变化被 _index_sync_tick 侦测
               ▼
        INDEX.md 自动刷新（"我知道"更新）
        → 下一次路由比这一次更聪明（复利闭环）
```

**未命中路径**（INDEX 查不到合适 agent 时）：

```
INDEX 未命中
  ├─ a. 询问用户: "无现成 agent 承接 X 环节，是否新建/指派?"
  ├─ b. 用户同意新建 → dashboard 建 agent(生命周期钩子)→ INDEX 立即重建
  └─ c. 临时兜底: X 自己做 + 在会话末标记"路由缺口"进 session-audit
```

**降级路径**：目标 agent 标记 shell/dormant → 不路由，如实告知；call_agent 超时(默认180s) → 报告 blocker + 已尝试方法 + 替代方案（与现宪法 scraping policy 的诚实条款同构）。

---

## 7. 与现有机制的关系

| 现有机制 | 关系 |
|---|---|
| `_constitution_sync_tick` | 模式模板；宪法只新增指针条款，广播复用 |
| `_config_sync_loop`(5s) | 不动。INDEX 走独立慢 tick，避免 5s 热路径膨胀 |
| `_aggregate_usage_into_main` | 同族"回流"先例；⓺蒸馏回流是它的知识版 |
| `hermes_delegate_mcp` | 被 INDEX 激活的管道 ⓸；防自调/超时逻辑沿用 |
| `hermes_vault_mcp` | 被 INDEX 激活的管道 ⓹；Vault 地图数据源 |
| twin(steward/architect) | steward=蒸馏执行者候选；twin 不进名录主表(共享根记忆，标注为 X 的分身) |
| curator | 不冲突；INDEX 的 shell/dormant 标记可反哺归档瘦身决策 |
| kanban | 复合任务的步骤可落卡；非依赖，不强制 |
| selfcheck.py | 增 1 条 INDEX 新鲜度断言 |

---

## 8. 实施清单（最小原型 → 完整）

**Phase 1 · 最小原型（不含 LLM，纯确定性，预计半天）**
1. `server.py`: `_build_index()` + `_index_sync_tick()` + 生命周期钩子 + `POST /api/index/rebuild`；
2. 宪法面板新增 `[rule:context-index]` 条款（内容操作，无代码）；
3. INDEX 手写覆盖区先写路由表初版（~10 行）；
4. `selfcheck.py` 增断言；改完跑 `launchctl kickstart` + selfcheck 全绿。

**Phase 2 · 语义蒸馏**
5. steward oneshot 批量生成"我知道"一行摘要（增量、脏检查）；
6. 路由表从名录自动导出，手写区变为覆盖而非唯一来源。

**Phase 3 · 回流闭环**
7. `session-audit` skill（hermes-skill-factory 制作）：会话末蒸馏三分法写入；
8. dashboard 加 INDEX 面板（可视化名录+路由表+手动重建按钮）。

**验收标准**：任选一个非根 agent，给它一个明显跨域的任务，观察它是否 ①主动读 INDEX ②选择正确管道 ③对 shell agent 如实降级。三条全过即闭环成立。

---

## 9. 边界与风险

- **INDEX 污染宪法**：坚决不把名录放进宪法/SOUL——只放指针。
- **蒸馏泄密**：生成器白名单字段 + 密钥模式过滤；记忆原文永不进 INDEX。
- **空壳误路由**：shell/dormant 状态强制标注 + 宪法禁止强行路由。
- **生成器与手改打架**：受管块 + 手写覆盖区分离（同宪法块模式），生成器只碰受管块。
- **`hermes update` 回灌**：INDEX.md 在 `~/.hermes` 根，属数据非代码，不受 git clone 更新影响；生成逻辑在 server.py 内，随 dashboard 版本走。

---

## 10. 长期维护方案（已落地 + 演进触发）

> 本节是索引层"活下去"的契约。核心理念：**索引永远只是各源文件的影子**——源变影随，影坏可弃（删掉自动重建，不丢任何原始信息）。

### 10.1 三层新鲜度（谁保证 INDEX 不过期）

| 层 | 保证什么 | 机制 | 状态 |
|---|---|---|---|
| **结构新鲜度** | 名录/上级/状态/空壳标记跟随组织变化 | `_index_sync_tick` 签名脏检查（`hierarchy.json`/`labels.json` + 各 `SOUL.md`/`MEMORY.md` 的 mtime），5s tick 纯 stat，变了才重建 | ✅ 已落地、常驻 |
| **内容新鲜度** | "我知道"一行的语义质量 | 一期=确定性桩（persona 首段 + 记忆条数）；二期=steward 增量蒸馏 | ⏳ 一期已落地，二期待触发 |
| **手写新鲜度** | 任务路由表与现实对齐 | 人工维护覆盖区；selfcheck 附加检查路由表引用的 agent 是否仍存在（软提示，不判红） | ✅ 已落地 |

### 10.2 失效检测（tick 死了怎么发现）

`selfcheck.py` 的 `上下文索引 INDEX.md` 断言不靠脆弱的"生成时间<24h"（稳定系统本就长期不重建，时间阈值必然误报），改用**漂移检测**：

- **硬红**：INDEX 中出现的 agent id 集 **必须等于** 磁盘上真实 profile 集。任一方向不等（磁盘新增没进 INDEX / INDEX 残留已删 agent）= tick 未运行或落后或 INDEX 被手改破坏 → 报红并指向 `POST /api/index/rebuild`。这是"结构变了 INDEX 有没有跟上"的确定性信号。
- **软提示**：手写路由表里引用了磁盘上不存在的 agent（改名/删除后路由表未更新）→ 附在 detail 里提醒人工核对，不判红（可能是为未来 agent 预留）。
- **既有结构校验**：受管块可渲染、`END` 标记完整、覆盖区非空。

### 10.3 生命周期覆盖（一个设计决策）

原设计（§4）写的是"生命周期钩子直接调用重建"。**实现改用签名脏检查取代显式钩子**——因为建/删/改名/移树/空壳注魂/活跃⇄休眠**全都会改** `hierarchy.json`/`labels.json`/`SOUL.md`/`MEMORY.md` 的 mtime，签名天然捕获，无需在每个生命周期端点插一行。更少的耦合点、更难漏。代价：最长 5s 延迟重建（可接受；需即时用 `POST /api/index/rebuild`）。

### 10.4 失败恢复（永不击穿）

- `_index_sync_tick` 用 `except Exception: pass` 包裹——**索引维护绝不击穿 5s 主循环**（与 `_constitution_sync_tick` 同规格）。
- 生成失败时 INDEX.md **保留上一版好文件**（`write_index` 原子写 `.tmp` + `os.replace`，写坏也不留半截）。
- **删掉 INDEX.md** → 下个 tick 自动重建（源文件在，影子随时可再生）。
- **覆盖区丢失** → `write_index` 检测到无手写内容时自动补种默认路由表（`_DEFAULT_OVERRIDE`）。

### 10.5 责任边界（机器区 vs 人工区）

- **机器区**（`MANAGED_BEGIN … MANAGED_END`）：生成器全权，人勿手改（改了下次重建即被覆盖）。
- **人工区**（`END` 之后）：人全权，机器永不触碰。任务路由表、固定语义、临时覆盖都放这。
- 铁律：**人只在 `MANAGED_END` 之下编辑**。selfcheck 守这条边界的完整性。

### 10.6 维护动作速查

```bash
# 手动立即重建（生命周期后想即时生效，不等 5s tick）
curl -X POST http://127.0.0.1:8877/api/index/rebuild
# 或离线重建
/opt/homebrew/bin/python3.11 ~/.hermes/dashboard/index_builder.py ~/.hermes
# 体检（含漂移检测）
/opt/homebrew/bin/python3.11 ~/.hermes/dashboard/selfcheck.py
# 改了 server.py 的 tick/端点后必须
launchctl kickstart -k gui/$(id -u)/com.hermes.dashboard.backend
```

### 10.7 演进触发条件（Phase 2/3 什么时候做，而非"有空就做"）

- **触发 Phase 2（steward 语义蒸馏）**：当"persona 首段 + 记忆条数"不足以让路由准确——即出现**误路由**（把任务派给领域描述含糊的 agent）或活跃 agent 数 > ~15 导致名录一行摘要太粗。届时让 steward 对**脏 agent 增量**跑 oneshot 生成"我知道"，绝不全量烧 token。
- **触发 Phase 3（session-audit 回流）**：当发现"同一条偏好/事实反复要人重讲"——说明会话结束没有沉淀。届时做 `session-audit` skill，按三分法写回（全局→根记忆 / 领域→本 agent / 沉淀→vault），mtime 变化自动触发 §10.1 结构层刷新，形成复利闭环。
- **不触发就不做**：一期确定性索引若已让跨域路由稳定工作，Phase 2/3 是纯增益，不是欠债。
