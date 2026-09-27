# 裁决记录：批次五十二 组内名字与三处修复（2026-09-21，AD-173）

**背景**：第三次外派的真机报告 `docs/quality/reports/2026-09-21-group-3/REPORT.md`。房间循环本身三条路全过（L21 通过 = AD-172 真机成立），这一批只修它周边的五处：新建组不能直接发、同名成员的 `#2` 没跟到两处、「加载更早」到头还要多点一次、Context Packet v0 跟不上房间、改名找不到入口。

五件里只有最后一件需要裁决——其余四件都是「已有口径没落到某一处」，改法由那条口径自己决定。

**验收**：kernel **1838 / 79 skipped**（批次五十一基线 1829/79，+9 条），仓库级 246（不变），前端 **573**（基线 562，+11 条），`npm run check` 全绿。**真机未验**——测试单新增 **L22**，L9 / L13 各补一句，在 `docs/quality/test-card-batch40-density.md`。

---

- **AD-173 「他在这个组里叫什么」写成员的 `roleLabel`，不碰会话标题；改会话标题另有一条路。**

  ### 这一条要回答的是「有两个名字」

  真机报告 §5「藏过头了」第 3 条：测试员找不到会话改名入口。编排方复核说「实际在侧栏会话行 ⋯ 菜单里（`CtxMenu.tsx:87-89`，走 `renameAgent`），要确认它改的是组读的那个标题」。**查下来不是**：

  - `renameAgent` → `POST /api/agent/{name}/rename`，body `{display}`，落在 legacy `server.py:3578`（`_set_label`，写 `labels.json`）。它改的是 **agent profile / 项目的显示名**，底层 profile ID 不变。
  - `CtxMenu` 挂在 `App.tsx:741`，`node` 来自 `net.nodes[menu.name]`（一个 `OrgNode`），`openMenu` 由 `Sidebar` 的**项目行**与 `AgentGraph` 的**节点**调用。**侧栏的会话行（`ConversationSidebar.tsx`）根本没有 ⋯ 菜单。**
  - 所以它与 `memberDisplayName` 回退链里那个「会话标题」毫无关系，测试员找不到入口不是可发现性问题，是**那个入口当时确实不存在**。

  于是这一批要造的是**两个**入口，因为这里本来就有**两个名字**：

  | 名字 | 改哪儿 | 影响范围 |
  |---|---|---|
  | 会话自己的名字 | `PATCH /api/conversations/{id}` 的 `title`（本批新收） | 全站：侧栏、会话页、以及所有以它为回退档的地方 |
  | 他在这个组里叫什么 | `PATCH /api/groups/{id}/members/{m}` 的 `roleLabel`（本批新端点） | 只有这个组 |

  ### 为什么组内名字是 `roleLabel` 而不是别开一个字段

  显示名的回退链是 `roleLabel > 会话标题 > 成员 id 尾段`（`resolve_display_names`），`roleLabel` **已经在第一位**。它此前只在加入 / spawn 时由 `role` 参数写一次，之后没有任何改法——也就是说「这位在这个房间里的名字」这个概念早就存在，缺的只是一条改它的路。新开一个 `groupDisplayName` 字段会让同一件事有两个真源，而回退链得在它们之间再排一次序。

  这也正好答了「改完会不会牵动别处」：不会。同一条会话可以在这个组里叫「评审」，在侧栏仍旧叫它本来的名字——**这是特性不是妥协**，一个人在不同房间里被叫不同的名字是常识。

  ### 落法

  - **`PATCH /groups/{id}/members/{memberId}`**，body `{roleLabel}`。空字符串 = 清掉（名字回退到会话标题）；**不给这个键 = 400 `empty_patch`**——「清掉」与「没提」必须分得开，否则一次只想改别的字段的 PATCH 会顺手把名字抹掉。已经叫这个名字了就是一次无操作，也不往时间线上添一行没内容的话。
  - **改完重算那张共用表**，时间线上那一行写的是**重算之后**的名字：用户敲「写手」而组里已经有一位写手时，他接下来真的会被告知的名字是「写手#2」，界面上的回执与时间线说的都必须是后者。用户敲的是意图，不是结果。
  - **时间线一行 `kind="system"`「旧名 现在叫 新名」**（`metadata.change = "member_renamed"`）。两个名字都写出来：显示名同时是称呼与地址，改名之前的发言仍旧署着旧名字，只说新名字的话用户读不出这两串是同一个人。
  - **组级流新增 `change: "member_renamed"`**，而不是复用 `group_updated`：改名同时改了**地址**（`@名字` 解析回成员 id 的那张表），浮窗除了重取成员还要把 `@` 菜单与房间说明一并刷新。
  - **界面**：成员「…」菜单多一条「在本组叫什么」，点开一个小弹窗（现在的名字先填好，留空 = 恢复成会话标题）。已经走了的成员也给这一条——他还可能被重新加入，那时叫什么已经定好了。
  - **`room.py` 一个字没动**：`resolve_display_names` / `resolve_mentions` / `next_round_queue` / `advance_thread` 全是只读调用。改名之后 `@新名` 能点到人，靠的就是那张表本来每次现算。

  ### 会话标题那一条（同批，无需裁决）

  `PATCH /api/conversations/{id}` 多收一个 `title`，**在取 Binding / 取 Driver / 问能力表之前就处理完**：那几步是给「会话级模型」准备的，而给一条会话改个名字与引擎毫无关系——塞在后面会让一台不支持会话级模型的引擎上「改名」吃一个 501 `conversation_model_unsupported`。空标题 400 `empty_title`（列表里那一行总要有字）。入口是会话页**页头那行标题本身**（点一下就地改，Enter 存 / Esc 收）——页头不因此多一枚按钮（★H：工作区中性，强调色只给运行点）。

  **legacy `server.py` 一个字没动**：`renameAgent` 与它的端点照旧管项目显示名，那是另一件事。

- **另外四件（口径已有，只是没落到某一处）**

  - **新建的组加完人能直接发**（`GroupDock.tsx`）。默认勾选原来是「每个组只种一次」，而新建组第一次到达时 `members[groupId]` 是**空数组**（不是 `undefined`），那一刻种下的是一个空勾选并记作「已种」。改成按**变化**对账：空变非空才算种过；新出现的 active 成员补勾，但绕开「用户亲手取消过」的那几位（`targetsUnchecked`）；刚从 active 掉下来的去掉，而本来就不是 active 却勾着的留着（批次二十八：暂停的成员勾得动）。分得清后两种靠的是上一次的状态表。
  - **同名成员的 `#2` 跟到系统行与勾选框**（真机 UI-03）。时间线上的成员变更行改用 `resolve_display_names`；移出那一条**要先取名字再动手**（`list_for_collaboration` 默认不带 `left`，人一走名字就不在表里），所以 `_notify` / `_append_system_line` / `_leader_left` 多收一个 `display_name`。`group.compose.target` 的 `{title}` → `{name}`，值改用后端算的显示名。
  - **翻到含 sequence 0 的那一页就是到头**（真机 UI-04）。`nextBefore` 原来照回本页最早行的 sequence，那一行是 0 时也回 0。组内 sequence 从 0 起，所以「最早行 == 0」是精确判据，没有用「这一页少于 limit 条」那种会看走眼的启发式。前端一个字没改。
  - **Context Packet 跟上房间**（真机 UI-01 / UI-02；产品方 2026-09-21 裁定**修，不收**）。**AD-154 一个字没改**——仍旧是只读汇编、仍旧不注入，换的只是三处取数的出处：名字用那张共用表；`lastAssistantMessage` = 组时间线上他最近一条 `member_turn` 的正文（`passed` 的跳过）；`lastUserMessage` = 最近一条**点到他**的用户消息（`targetMemberIds` 含他，或没写目标而正文点了他的名，或没写目标且谁也没点到 = 发给全体）；事件流那条路只留 `lastCompletedAt`（一次 run 什么时候跑完，只有会话自己知道）。
    原来那两句为什么是错的：v0 摘的是**成员自己那条会话的事件流**，而在房间里投给成员的「用户消息」正是**房间增量**，里面装着别人的发言——一张本该让人一眼看清「谁到哪儿了」的表，给出的是一个会误读的答案。

- **wire 变化**：`ContextPacketMemberWire` 多一个 `displayName`（前端渲染它，取不到退回 `title`）；`GroupChange` 多一个 `member_renamed`；`PatchConversationBody` 多一个 `title`。端点新增一条（`PATCH …/members/{m}`），没有删字段、没有改信封（`AgentEventEnvelope` v1.1 没动）。

- **行为变化（既有端点）**：① `GET /groups/{id}/messages` 的 `nextBefore` 在最早行为 0 时变成 `null`（此前是 0）；② `GET /groups/{id}/context-packet` 的 `lastUserMessage` / `lastAssistantMessage` 换了出处与含义（markdown 里的标签也跟着改成「最近一条点到它的话」／「它在组里最近说的」）；③ 成员变更的系统行带上了 `#2`。三条都是**修正**，不是新口径。

- **待办**：① 真机未验（测试单 L22，L9 / L13 各补一句）；② 组内改名与**房间说明的缓存**没有交叉验过——改名之后下一轮投递才会带上新名片，正在跑的那一轮里成员手上仍是旧的，这是对的但真机上没人看过；③ 会话标题的改名入口只有会话页一处，侧栏会话行仍旧没有 ⋯ 菜单（本批没造，那是另一片的事）；④ 成员正文里的 `@名字` 仍未渲染成 pill（AD-169 待办 ③，照旧没做）。
