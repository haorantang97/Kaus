# 裁决记录（续三十四）：批次三十七 外部评审后端修复（2026-09-07）

**背景：** `docs/quality/external-review-2026-09-07.md`（独立评审，9 项 + 7 条产品判断）。
本批只做后端那一半：R2 / R3 / R4 / R5 / R6 / R7 与产品判断 #4 / #5。前端的 R1 / R8 / R9 另派。

**取证边界：** 全部复现与验收都在容器里用**临时 HERMES_HOME、MockDriver 与仓库自带的假 ACP
子进程**跑，一次真实引擎、一份真实凭据都没有碰过。评审自己的复现包也是同一条纪律。

---

## AD-161 单调安全合并补三条规则（续 AD-150，不改历史）

AD-150 立的规矩没有错，错的是它落地时留下的三个洞——评审在**真的 Resolver 上**逐条复现过。
这三条一起构成「子级只能收紧」这句话真正成立的条件：

- **（a）合并必须发生在字段级，且先拆 `{"value": {...}}` 那层壳。** 导入器落库的形状是
  `{"value": {...}}`（AD-42 整键粒度），而 `monotonic_child_wins` 从前在**外层**
  `dict.update`：父项目 `enabled=false / max_depth=1 / max_concurrent=1`，子项目只在
  `value` 里改一个 `default_model`，父项目那三条约束会被整段顶掉，投影再按默认值补上
  `enabled=true`——**用户以为只改了模型，实际把委派重新打开了，而且一条 warning 都没有**。
  新增 `monotonic.merge_fields()`：两边各自拆壳、按字段合并、按祖先那一份的形状封回去。
  这也是这个洞最阴的地方：单调检查本身当时是「通过」的——它检查的那几个字段在子级那份
  config 里**根本不存在**，于是一个字段都没被拦，而三条约束照样消失了。

- **（b）类型不同不是「放行」，是「判断不了」。** 旧的 `tighter_or_equal` 对类型对不上的
  取值一律返回 `True`（AD-150 的「看不懂就放行」）。真机上的后果是：子级提交字符串
  `"true"` / `"9"`，单调检查因为类型不同放它过去，随后 Hermes 的策略模型（pydantic 宽松
  模式）又老老实实把它们转成 `True` / `9` 投影下去——**检查看的是一种形状，真正生效的是
  另一种，那这道检查等于不存在**。改法不是自己再写一张小转换表（那会长出第三种形状），
  而是**用与策略模型同一套转换**（`TypeAdapter(bool)` / `TypeAdapter(int | float)`，正是
  `DelegationPolicy` 字段在用的那一套）先规范再比较，并有一条参数化用例钉住「两边转出来
  的值必须相等」。转不出来的取值（`"whatever"`、乱七八糟的枚举值）**驳回并保留祖先值 +
  记 warning**，不再放行。
  - **旧口径只在安全字段上收窄。** AD-150 那句「看不懂就放行」对**非安全**键原样保留：
    `SAFETY_FIELDS` 之外的字段、以及不走单调策略的能力类型，仍然是 child-wins。这一点
    有独立用例守着——收窄的是这张表管得着的那五个旋钮，不是所有编辑。

- **（c）`null` = 继承，不是解除。** 子级把一个安全旋钮写成 `null`，语义是「这一层不说
  话」，键从子级的贡献里摘掉，祖先那条约束在字段级合并里原样留下。**它从来不表示「取消
  这条上限」**——能解除祖先约束的动作只能发生在祖先那一层。旧代码把 `null` 当成一个「看
  不懂的取值」放行，于是 `max_depth: null` 直接让数值限制消失，而策略模型里 `None` 的意思
  恰恰是「本层不设限」。这一条不记 warning：它是一个**有定义的写法**，不是越界。

**为什么不报错**：AD-150 那条「越界保留祖先值 + 记 warning，不报错」原样保留。报错会让一次
正常的能力编辑整个失败，而用户其实只是在一个字段上越界了。

**信封仍然冻结**；**wire 变化：** 无新增字段与端点。`GET /api/projects/{id}/capabilities`
的 `warnings[]` 在上述 (b) 情形下**会多出条目**（既有的可选数组，前端已在渲染），
`entries[].config` 在祖先设过约束时**会多出祖先的键**——那正是修复本身。

**待办：** `merge_config_child_wins`（`runtime-config`）仍然是外层键级合并，它没有安全语义，
本批不动；真的要统一成字段级得先定 `{"value": ...}` 这层壳在公共 schema 里到底算不算一层。

---

## AD-162 保留期是「事件类别 × 这台引擎有没有原生历史」的函数

评审 R4 的复现：假 ACP 跑完一轮、关掉运行实例、时钟推进八天 → 原有 9 条事件全被
清理，恢复 0 条；Conversation 与原生会话关联都还在，**用户找得到这条会话，却看不见
任何内容**。

根因是两条各自都讲得通的规则叠在了一起：

- 站内 Event Store 按 D-16 / v1.0 §11.3 是「短期重放缓冲 + 渲染缓存」，整表可删，
  因为**内容能从原生历史重建**；
- ACP 协议里没有「读取历史条目」这个方法（`session/load` 是让 agent 自己重放到
  一条新会话里，不是把条目交出来），所以 ACP Driver 对 `load_native_history`
  明确抛 `UnsupportedCapabilityError`。

于是对这一整类引擎，「可重建」这个前提**不成立**，而清理照旧按它执行。

**规则：** `expires_at` 由 **(事件类别, 这条会话背后 backend 的 `sessions.history`)**
两者共同决定：

- 引擎的 `sessions.history` **不是 supported** 时，**正文类**事件
  （`message.*` / `tool.*` / `run.*` 以及产品自己那两条 `kaus/user.message`、
  `kaus/model.adopted`）保留到**这条 Conversation 被删除或归档**为止；
- **诊断类照旧过期**（`diagnostic.notice`、Backend 私有的未知扩展帧）——这条规则不是
  「什么都不删了」；
- 引擎能交回原生历史时（Hermes、Mock）**一切照旧**：站内这份确实只是缓存。

**几处刻意的选择：**

- **`unknown` 与 `unsupported` 同等对待。** 这是能力矩阵自己的口径（`bool(state)`
  只有 `supported` 为真，「没实测过的能力不该被当成有」），而且在保留期这件事上两
  种错误的代价不对等：多留一段本可以删的事件，比删掉一段没有第二份的历史轻得多。
- **不给 `expires_at` 开一个「永不过期」的空值。** v1.0 §8.5 不接受「无保留策略」
  这种表述。落库的是一个明确的时刻常量 `RETAIN_UNTIL_CONVERSATION_GONE`
  （9999-12-31Z），语义是「这批事件的保留期 = 这条会话的生命周期」，由 AD-105 的
  `DELETE /api/conversations/{id}`（`purge_conversation`）与归档路径回收。
- **判据是能力矩阵那一格，不是 Driver 的类名。** 加一台新引擎不该让保留期这条规则
  重写一遍；ACP 这一整类之所以命中，是因为它的能力表如实说了自己没有这一项。
- **决定发生在写入那一刻。** 清理仍然是一句 `WHERE expires_at <= now`，不需要为它
  开一条能按会话回查 Binding 的清理路径（那会让清理反过来依赖 Driver Registry 与
  领域库）。问不出能力时（Driver 没这个方法、探测抛错）传 `None`，按老口径走 TTL：
  不知道就不改既有行为（N §13.1）。

**信封仍然冻结**；**wire 变化：** 无。这是落库时的一列的取值变化，端点形状与字段
一个都没动。

**待办：** 归档（`state="ended"`）目前**不**回收这批事件——AD-148 的归档语义就是
「事件缓冲一条不清」，所以这里如实写成「删除或归档」中的删除那一半才真正回收；
真要为归档定一个更短的保留期，得先有人回答「归档之后用户还想不想看见它」。
另：已经按七天写进库的老行不会被追认，这条规则只对**本次改动之后写入**的事件生效。

---

## R5 旧写接口统一鉴权：一层 middleware + 一条启动自检

评审复现：抽出 `api_daemon_start` 放进隔离 app，用**跨站 Origin + 普通表单
Content-Type + 无认证头**发请求，200，并到达模拟的进程启动记录器。原因是新的会话接口
走 Origin + 本地 token 两关（D-17 / AD-66），而 `server.py` 里 58 条写路由直接挂在宿主
FastAPI 上，一关都没过。

**做法：** `session_bootstrap.install_legacy_write_auth(app)` 装一层 HTTP middleware，把
**同一份** `SessionAuthPolicy` 套到所有 `/api/` 下的 `POST/PUT/PATCH/DELETE` 上；读接口
一律不动。`server.py` 里那一段与 `session_host_v1` 开关**无关**——旧接口在不在闸内，不该
取决于新功能开没开。

**为什么是 middleware 而不是逐条加 dependency：** 8400 行、58 条写路由，逐条改既容易漏，
也会让「以后新写的那一条」默认不受保护。middleware 是**默认拒绝**的形状：新加的写路由自动
在闸内，忘了加装饰器不会变成一个洞。**安全覆盖不该等文件重构完**（评审原话）——这一条同时
回答了产品判断 #3 的顺序问题：先保护，再拆文件。

**为什么会话 router 自己那层 dependency 不撤：** 同一份判断跑两次是幂等的；撤了就得靠
「middleware 永远先跑」这条约定，那不是安全该依赖的东西。

**启动自检：** `unprotected_mutating_routes(app)` 列出闸盖不住的写路由（判据只有一条：
路径不在 `/api/` 之下），启动时打日志，并由 `tests/test_batch37_legacy_write_auth.py` 断言
它对真实 `server.app` 为空——这条断言才是防止「以后又有人在 `/api/` 外面挂一条写路由」的
那道门槛。

**已知未覆盖：** 两条 WebSocket（`/ws/chat`、`/ws/terminal-lab`）不是 HTTP 写方法，本批没有
纳入；AD-14 计划在 Phase 4 真机验收后把它们连同 `/api/pty` 一起删掉。这一条如实写进
`docs/ops/security.md` §4，而不是假装闸是全的。

**wire 变化：** 旧写接口现在会回 401 `unauthorized` / 403 `forbidden_origin`
（形状与会话接口一致：`{"error": {"code", "message"}}`）。**前端若还有任何直接打旧写接口
的地方，必须带上 `Authorization: Bearer <token>`**（token 取自既有的
`GET /api/session-auth/bootstrap`）。副作用一条：向不存在的 `/api/` 路径发写请求，现在先回
401/403 再谈 404。

---

## R7 「忙」是公共层的词汇，不是某一家 Driver 的私有概念

评审复现：假 ACP 的第一轮保持运行，经真实 HTTP 路由再发一句，得到
`500` / `internal_error` / `服务端出错：RuntimeError`。而同一个状态在 Hermes 那边
（批次二十七第 2 件）早就是 `409 turn_already_running` + 一句「等这一轮结束再发，
或先点停止」。

**做法：** ACP 的 `send_message` 改抛 `TurnAlreadyRunningError`（`drivers.base`），
并带**共享**的 `FailureHint`。code 与文案从 `drivers/hermes/failure_hints.py` 搬到
`drivers/base.py`（`TURN_ALREADY_RUNNING` / `turn_already_running_hint()`），Hermes 那一份
改为转调——**同一个状态在两台引擎上说两句不同的话，用户会以为自己碰到的是两件事**。

判断「它现在忙不忙」仍然是各 Driver 的私有知识（Hermes 认网关回的那几个词，ACP 看
自己手上的 `prompt_task`）；搬上来的只有「忙这件事本身」的公共词汇。这与 AD-151
「Driver 一行产品名都不许有」是同一条纪律的两面：私有知识留在 Driver 里，公共词汇
放在公共层——纯净性断言也确实在这一轮上红过一次（公共文件的注释里写了引擎名字），
按它改了。

**wire 变化：** ACP 那条路的 `POST /api/conversations/{id}/messages` 从 `500
internal_error` 变成 `409 turn_already_running`（形状与 Hermes 完全一致，前端已有分支）。

---

## R6 被引擎拒绝的那一句：不回滚，但要留下状态

评审复现：模拟引擎忙，事件库里仍有一条普普通通的 `kaus/user.message`；换成真 ACP +
假子进程经 HTTP 复现时，库里两条用户正文、只有一个 `run.started`。刷新之后那句话与
被正常处理过的一模一样，重发还会显示两遍。

**AD-105 的口径不变：不回滚。** 那句话确实被说出来过，落库的顺序也是对的（它是**发起**
下一轮的东西，必须排在 `run.started` 之前）。撤掉一条已经广播出去的正文，会让「这条
会话里说过什么」在不同客户端上给出不同答案。

**补的是一条状态事件：** `kaus/user.message.failed`，`data = {clientRef?, code, message}`。

- **同一个出口、同一个 namespace**（`extension.event` / `kaus`，与 AD-86 的 `user.message`、
  AD-155 的 `model.adopted` 一致）——信封 v1.1 一个字没动；
- **同一个保留期**（进 `PRODUCT_CONTENT_EVENT_NAMES`）：它是那条正文的状态，不是诊断。
  按 24 小时清掉，「刷新之后仍然看得见这句话没被处理」就只成立一天；
- **不新建卡片。** Reducer 只把已有那条用户消息标成 `delivery_status="failed"` 并记下
  `code` / `message`。新建一张卡会让「这句话说了几遍」变得答不出来；
- **配对按 `clientRef`**（AD-94 的对账编号，前端本来就用它换本地占位），没给才退回
  「最后一条还没被标失败的用户消息」。**配不上就什么都不做**——凭空标一条别的消息比不标更糟；
- **字段名不叫 `status`**：`MessageItem.status` 已经被「流式到哪了」占着，两个 status
  挤在一张卡上，读的人只能靠猜。
- **Host 照旧把异常抛回去**，没有把失败吞成「已发送」。

**接入层：** `POST /api/conversations/{id}/messages` 在**用户消息已经落库之后**的那几条
失败（`409 turn_already_running` / `501` / `502 message_rejected` / `409 runtime_not_active`）
的错误体里多一个 `detail.clientRef`——前端据它把本地那条置灰，而不必去猜是哪一句。

**wire 变化：**
- 新增扩展事件 `kaus/user.message.failed`，`data = {clientRef?, code, message}`。**前端需要
  在 `extensionCards.ts` 的白名单里登记它**（AD-71：未登记不渲染），并让 reducer 把对应
  `clientRef` 的用户消息置灰 + 显示 `message`、给一个重发入口；
- 上述几条错误体的 `detail` 多一个可选键 `clientRef`（没给编号时不出现，不是 null）。

**待办：** 「重试」按钮本身是前端的事（后端不需要新端点：重试 = 再发一次，带一个新的
`clientRef`）；`pending` 这一档没有做——它要求把「发出去了但还没落终态」也持久化，那是
另一件事，AD-105 的范围内先只区分 `ok` / `failed`。

---

## AD-163 `deny` → `bypass` 的改名记在案，本批不改

评审的产品判断 #5：`kernel/drivers/hermes/approval_map.py` 把公共值 `deny` 映射为
`off`，含义是**全部放行**。中文界面已经作了说明，但公共 API、未来适配器和维护人员
仍会自然地把 `deny` 理解为拒绝。

**同意这个判断。** 这是一个**语义反转的安全取值**——它不是文案问题：AD-150 之所以要
把审批档的强度顺序写成数据（`ask` > `auto` > `deny`）而不是留给读代码的人推，理由就是
这个名字读不出方向。一个需要靠注释才不会被读反的安全取值，长期成本是不断有人在它上面
犯同一个错误。

**但本批不改。** `deny` 已经在公共 wire 上（`PATCH /api/bindings/{id}` 的 `approvalMode`、
能力矩阵、投影报告、前端词典）。把一次破坏性重命名混进一批安全修复里发出去，会让这一批
的验收范围变成两件事；而且真机上还有一份用户配置在按现有取值跑。

**下一个允许破坏 wire 的批次要做的事（这就是 AD-163 的内容）：**

1. 公共取值改名 `deny` → `bypass`（`drivers.base.APPROVAL_MODES` 与本表同步改）；
2. **显式兼容层**：读入两个值都认（`deny` 视为 `bypass` 的历史别名），写出只用新值；
   Binding 的 `runtime_config` 里存量的 `deny` 由一次迁移改写，不靠「读的时候顺手翻译」
   长期共存——两个取值同时活着，就永远有人在写老的那个；
3. 迁移校验：一条断言「领域库里不再有 `deny`」+ 一条断言「老客户端发 `deny` 仍被接受」；
4. 前端词典两语各改一行；能力矩阵与投影报告里的取值同步。

**本批实际做的：** 在 `approval_map.py` 的模块 docstring 里加一段 `.. deprecated::`
——把「它现在是什么意思、为什么要改、改成什么、在那之前不要按名字推断语义」写在
**读代码的人一定会看到的地方**，而不只是躺在一份裁决记录里。

**wire 变化：** 无。本条是一份记录加一段注释。

---

## AD-164 已复现的版本缺陷可以按住一位怪癖——只往关的方向（AD-151 的唯一例外）

评审的产品判断 #4：「把引擎自报能力放在最高优先级，可能覆盖已经掌握的该版本失败事实；
简报中的 deepseek-acp 续接问题就值得重新裁决。」

**同意，并且这正是 AD-158 自己留下的那条尾巴**：`deepseek-acp` 0.8.0 的
`sessionCapabilities` 里有 `resume`、`loadSession` 也报 `true`，但两条 RPC 在同进程里
都回 `-32603`。目录因此填 `false`——可那两位**在连上的那一刻会被 `effective_quirks`
用引擎的原话翻回 `true`**（AD-151 的三层优先级），于是真机上续接照样失败，而界面上那两个
入口一直亮着。AD-158 当时如实写下了这个结果，并明确拒绝为它开一个「预设压过 initialize」
的后门——**那个判断没有错，错的是当时只有「后门」与「不管」两个选项**。

**机制（窄到不能再窄）：** `AcpPreset.known_bad: Mapping[str, tuple[str, ...]]`，
`适配器版本前缀 -> 必须强制关掉的怪癖位`，在 `effective_quirks` 的**最后一步**生效。

四条限制，每一条都是为了让这个例外不长成一般规则：

1. **只能关，不能开。** `forced_off_bits()` 只产出位名，调用方一律赋 `False`。
   预设写的是「我们猜它行不行」，可以被引擎自己的话覆盖；`known_bad` 写的是「这个版本上
   我们**实测它不行**」——那不是猜测，而是事实里更具体的那一部分。反方向（凭一张手写表
   声明一项引擎没说过的能力）仍然是被禁的，理由与 AD-151 一字不改。
2. **按版本前缀匹配，且读不到版本就一位都不关。** 一个已复现的实现缺陷通常横跨整条补丁
   线（`"0.8"` 命中 0.8.0 / 0.8.3），逐个版本号登记只会漏；反过来，`agentInfo.version`
   读不到时我们并不知道装的是哪一版，凭空关一位与凭空开一位一样是编造（N §13.1）。
3. **登记的位在目录里必须本来就是 `false`。** 有断言守着：登记一个目录值为真的位去
   「强制关掉」，说明这一行的目录值和这条例外在互相打架，那是配置错误不是特性。
4. **这张表是「已复现的缺陷」，不是「我担心它有问题」。** 断言「目前只有
   `deepseek-acp` 一行有 `known_bad`」——空着才是常态；每加一行都要带取证出处，
   并在 `notes` 与 `docs/ops/backends.md` 里写清楚「什么时候该把它删掉」。

**产品上的效果**（这才是 #4 真正要的东西）：这条 Backend 上「在 CLI 里打开 / 续接」两个
入口**直接不显示**（AD-71 缺能力静默不显示），而不是亮着、点了回一个 `-32603`。
#4 说的「已复现的版本缺陷用于决定默认能否启用」，落到代码里就是这一位。

**wire 变化：** `GET /api/backends` / `GET /api/backends/{id}` 里 `deepseek-acp` 那一行在
连上 0.8.x 时，`quirks.supportsSessionResume` / `supportsSessionLoad` 与
`capabilities.sessions.resume` / `sessions.history` 相关格子从「被自报翻成 true」变回
`false` / `unsupported`。**没有新增字段、没有新增端点**：`known_bad` 是目录内部的数据，
**不上 wire**（它是我们对某个版本的判断，不是引擎的自述）。

**待办：** 只登记了 `deepseek-acp` 0.8.x 的 resume/load 这一格。#4 的另一半——对外把预设
分成「已验证可日常使用」与「实验接入」并各自标注引擎/适配器版本与最近验证日期——本批**没有
做**：那要在 wire 上加一根新的轴（`preset.tier` 之类）并带动前端，属于下一批。

---

## R5 补正（第二轮）：发 token 的那条路必须与闸同寿

前端评审在合并后复现出一个死结：`install_legacy_write_auth` 是**无条件**装的（这是 R5
刻意的选择），而 `GET /api/session-auth/bootstrap` 长在会话 router 上、由
`attach_session_api` 在 `DASH_FEATURE_SESSION_HOST_V1` 关闭时整段跳过——于是 flag 关着的
默认部署上，**旧写接口要一个拿不到的 token**，全线 401。

这是 R5 那条裁决自己的漏洞：它说对了「旧接口的安全性不该取决于一个与它无关的新功能开关」，
却没有把这句话推到底——**闸的前置条件（能拿到 token）也不该取决于那个开关**。

**两个不该选的修法，各自的理由：**

- 把闸也放到 flag 后面 → 等于取消 R5，默认部署上写接口重新裸奔；
- 把整个会话 API 提前挂上 → 会把 Session Host、Driver Registry、后台任务一并拖进来，
  flag 就失去意义了（`session_bootstrap` 模块文档的第一句就是「关掉 flag 时零影响」）。

**做法：** 把发 token 拆成一条**零内核依赖**的独立 router
（`app.api.session_auth.build_bootstrap_router`）——它只需要一份 `SessionAuthPolicy`
（= 一个 token 文件 + 一张 Origin 白名单），而会话编排需要半个内核。**发 token 与会话编排
本来就是两件事**，此前只是碰巧住在同一个 router 上。`install_legacy_write_auth` 补挂它，
且**只补这一条**；flag 开着时按路径判重、不重挂。准入判断逐字未变（Origin 白名单 +
`Sec-Fetch-Site`，同一个 `issue_token`）。

顺带把 `SessionRuntime.auth_policy` 露出来，让 flag 开着时闸与会话 router 共用**同一个**
policy 对象——两份 policy 读的是同一个 token 文件，但同一个对象才能保证 Origin 白名单这类
取值不会哪天在两处配出两个答案。

**wire 变化：** `GET /api/session-auth/bootstrap` 在 `session_host_v1` 关闭时**开始存在**
（此前 404）。取值、形状、准入判断均未变。

**另：`kaus/user.message.failed` 的保留期。** 前端提到它像是按 24 小时的诊断档过期——**实际
不是**：R6 那一批已经把它放进 `PRODUCT_CONTENT_EVENT_NAMES`，两条路径（`app.events.models`
的规则函数与 `EventStore` 的服务层副本）都返回 7 天。本轮不改行为，只把断言从「两个函数返回
值相等」加强到**落库那一行的 `expires_at`**，再加一条「时钟推过 24 小时、清一次，它还在」——
之前那条用例比的是规则，现在比的是事实。
