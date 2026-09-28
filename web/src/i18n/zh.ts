/* 中文词典 = **键的真源**（`en.ts` 被钉成同一组键，漏一个就编译不过）。
 *
 * 批次十一：AD-100 兑现，`nav.*` 已按 IA §1.2 叫法表改成中文名（概览 / 项目 /
 * 仪表盘 / 任务板 / 资料库 / 仓库 / 设置）。flag 关闭时的导航标签也跟着变——
 * 像素基线随之更新，这是 AD-100 说好的。
 */

export const zh = {
  "conversation.actions": "更多",
  "conversation.archive": "归档对话",
  "conversation.restore": "恢复对话",
  "conversation.archived": "已归档",
  "conversation.stop.waitingForRun": "正在确认当前任务，请稍后再点停止。",
  "conversation.pending.check": "检查发送状态",
  "bar.defaults.prefix": "默认",
  "bar.defaults.hint": "项目的新会话默认值；当前会话沿用启动时的设置。引擎配置的应用状态可在项目中查看。",
  "conversation.defaults.saved": "已保存项目默认值。当前会话继续使用原有设置；引擎配置的应用状态可在项目中查看。",
  "bar.workspace.choose": "选择工作位置",
  "conversation.attach.unsupported": "此引擎尚未确认支持附件，文件未上传。可以先发送文字，待连接后重试。",
  "conversation.attach.tooMany": "每条消息最多添加 8 个文件。",
  "conversation.attach.tooLarge": "单个文件不能超过 10 MB。",
  "conversation.attach.failed": "“{name}”没有上传成功：{error}",
  "conversation.attach.list": "本条消息的附件",
  "conversation.attach.ready": "已上传",
  "conversation.attach.uploading": "正在上传…",
  "conversation.attach.remove": "移除 {name}",
  "conversation.attach.drop": "松开以添加文件",
  "conversation.draft.failed": "草稿暂时无法保存，请保留当前窗口。",
  "conversation.draft.running": "可以继续写下一条，草稿会保留。",
  "conversation.draft.saved": "草稿已保存",
  "conversation.composer.keys": "Enter 发送 · Shift + Enter 换行",
  "conversation.error.dismiss": "收起提示",
  "conversation.history.retry": "重新加载",
  "conversation.workspace.saved": "工作位置已保存，新会话使用这个目录；已有会话保留启动时的目录。",

  /* ---- 语言开关（放在主题切换旁） ---- */
  "locale.title": "界面语言",
  "locale.system": "跟随系统",
  "locale.zh": "中文",
  "locale.en": "English",
  "locale.short.system": "系统",
  "locale.short.zh": "中",
  "locale.short.en": "EN",

  /* ---- 左侧导航（AD-100 兑现：按 IA §1.2 叫法表改中文名） ---- */
  "nav.newConversation": "新会话",
  "nav.home": "概览",
  "nav.agents": "项目",
  "nav.new": "New",
  "nav.dash": "仪表盘",
  "nav.kanban": "任务板",
  "nav.vault": "资料库",
  "nav.warehouse": "仓库",
  "nav.config": "设置",

  /* ---- 通用 ---- */
  "common.close": "关闭",
  "common.pending": "需后端补",
  "common.pending.title": "写端点缺，见 information-architecture.md §8",
  "common.dash": "—",
  "common.expand": "展开",
  "common.collapse": "收起",

  /* ---- 会话侧栏 ---- */
  "sidebar.title": "会话",
  "sidebar.error": "会话列表加载失败：{error}",
  "sidebar.loading": "加载中…",
  "sidebar.empty": "暂无会话",
  "sidebar.running": "运行中",

  /* ---- 会话页 ---- */
  "conversation.title.fallback": "对话",
  /* batch52 第 5 件：会话标题此前没有任何界面入口——侧栏 ⋯ 菜单里那条「改名」
     走的是 `POST /api/agent/{name}/rename`，改的是**项目**的显示名。入口现在就在
     标题本身上（点一下就地改），所以这句话要说清楚改的是哪个名字。 */
  // batch52
  "conversation.title.rename": "重命名",
  "conversation.title.field": "会话名字",
  "conversation.title.failed": "没改成：{error}",
  "conversation.runState.idle": "空闲",
  "conversation.runState.running": "运行中",
  "conversation.runState.completed": "空闲",
  "conversation.runState.failed": "出错",
  "conversation.runState.interrupted": "已中断",
  "conversation.runState.external": "正在外部终端运行",
  "conversation.reconnecting": "连接已断开，正在重连…",
  "conversation.empty": "发第一条消息开始。",
  "conversation.message.label": "消息",
  "conversation.composer.placeholder": "继续这段对话…",
  "conversation.send": "发送",
  "conversation.pending.failed": "没发出去",
  "conversation.pending.retry": "重发",
  "conversation.pending.unconfirmed": "未确认",

  /* ---- 新会话草稿页 ---- */
  "draft.heading": "Let’s make something sick",
  "draft.sub": "选择项目与引擎，写下第一条消息。文字草稿会保留，附件将在发送时上传。",
  "draft.field.project": "项目",
  "draft.field.engine": "引擎",
  "draft.field.model": "模型",
  "draft.field.reasoning": "推理",
  "draft.unlock": "解锁以改项目",
  "draft.relock": "锁回来源项目",
  "draft.engine.loading": "加载中…",
  "draft.engine.none": "这个项目还没有接入引擎",
  "draft.reasoning.aria": "推理强度",
  "draft.reasoning.default": "默认",
  "draft.composer.placeholder": "说点什么…",
  "draft.send": "发送",
  "draft.sending": "发送中…",

  /* ---- 项目详情页 ---- */
  "project.nav.aria": "子项目 / 上级",
  "project.nav.title": "项目",
  "project.nav.loading": "正在读取项目树…",
  "project.crumb.root": "项目",
  "project.status.active": "运行中",
  "project.status.archived": "已归档",
  "project.status.disabled": "已停用",
  "project.action.newConversation": "新会话",
  "project.action.openTerminal": "到终端打开",
  "project.action.settings": "设置",
  "project.error": "项目读取失败：{error}",
  "project.terminal.ok": "已在终端打开这个项目",
  "project.terminal.fail": "到终端打开失败：{error}",
  "project.conversations.title": "会话",
  "project.conversations.loading": "正在读取会话…",
  "project.conversations.empty": "这个项目还没有会话。",
  "project.conversations.all": "查看全部",
  "project.conversationStatus.idle": "空闲",
  "project.conversationStatus.running": "运行中",
  "project.conversationStatus.paused": "已暂停",
  "project.conversationStatus.ended": "已结束",
  "project.conversationStatus.error": "出错",
  "project.more": "更多设置",
  "project.instructions.title": "指令与守则",
  "project.instructions.meta": "沿组织树分层继承 · 注入 SOUL 顶部",

  /* ---- 能力表（项目详情页 ③ 区） ---- */
  "capabilities.title": "能力",
  "capabilities.loading": "正在读取能力…",
  "capabilities.empty": "这个项目还没有任何能力。",
  "capabilities.col.type": "类型",
  "capabilities.col.content": "内容",
  "capabilities.col.source": "来源",
  "capabilities.col.action": "操作",
  "capabilities.source.local": "本项目",
  "capabilities.source.inherited": "继承自「{name}」",
  "capabilities.source.blocked": "在「{name}」禁止",
  "capabilities.overridden": "已覆盖",
  "capabilities.overridden.title": "链上 {chain} 都设过，最终用「{name}」的",
  "capabilities.footnote": "禁止后，本项目及其全部下级都不再获得这项能力（下级可以自己重新设置来恢复）。",

  /* ---- 已接引擎面板 ---- */
  "engines.loading": "正在读取已接引擎…",
  "engines.error": "引擎清单读取失败：{error}",
  "engines.write.ok": "{label}已生效",
  "engines.write.fail": "{label}失败：{error}",
  "engines.write.model": "改模型",
  "engines.write.reasoning": "改推理强度",
  "engines.write.makeDefault": "设为默认",
  "engines.write.detach": "解除挂载",
  "engines.write.attach": "接入引擎",
  "engines.detach.confirm": "解除挂载「{name}」？只删本站这条记录，引擎自己的会话与配置一个都不动。",
  "engines.detach.ok": "解除",

  "engine.panel.title": "已接引擎",
  "engine.panel.empty": "这个项目还没有接入引擎。",
  "engine.attach.aria": "接入引擎",
  "engine.attach.button": "接入",
  "engine.attach.disabled": "接入引擎",
  "engine.attach.none": "没有可接入的引擎",
  "engine.badge.default": "默认",
  "engine.badge.disabled": "已停用",
  "engine.probe.available": "就绪",
  "engine.probe.degraded": "降级可用",
  "engine.probe.unavailable": "未就绪",
  "engine.probe.unknown": "未探测",
  "engine.auth.signed_in": "已登录",
  "engine.auth.signed_out": "未登录",
  "engine.auth.unknown": "登录状态未知",
  "engine.action.newConversation": "新建对话",
  "engine.action.makeDefault": "设为默认",
  "engine.action.detach": "解除",
  "engine.action.signIn": "去登录",
  "engine.field.engine": "引擎",
  "engine.field.model": "模型",
  "engine.field.reasoning": "推理强度",
  "engine.field.nativeScope": "引擎侧身份",
  "engine.field.mode": "模式",
  "engine.field.nativeSessions": "原生会话",
  "engine.field.login": "登录",
  "engine.model.aria": "模型 · {name}",
  "engine.reasoning.aria": "推理强度 · {name}",
  "engine.value.unset": "未设置",
  "engine.value.unknown": "未知",
  "engine.reasoning.default": "默认",
  "engine.nativeSessions.count": "{count} 条",
  "engine.caps.toggle": "能力清单 · {count} 项",

  /* ---- 引擎配置展开区（AD-97） ---- */
  "engine.config.toggle": "引擎配置 · {count} 组",
  "engine.config.loading": "正在读取引擎配置…",
  "engine.config.empty": "这个引擎在本项目上没有专属配置。",
  "engine.config.keys": "{count} 个键",
  "engine.config.items": "{count} 项",

  /* ---- 能力分组名（按 capability_id 查；查不到用原键名） ---- */
  "capGroup.providers": "模型供应商",
  "capGroup.fallback_providers": "备用供应商",
  "capGroup.toolsets": "工具集",
  "capGroup.compression": "压缩",
  "capGroup.context": "上下文",
  "capGroup.prompt_caching": "提示缓存",
  "capGroup.auxiliary": "辅助模型",
  "capGroup.image_gen": "图像生成",
  "capGroup.agent": "代理行为",
  "capGroup.tool_loop_guardrails": "工具循环护栏",
  "capGroup.credential_pool_strategies": "凭据池策略",
  "capGroup.model": "默认模型",
  "capGroup.curator": "技能策展",
  "capGroup.delegation-extras": "委派（引擎专属）",

  /* ---- 能力取值（第 4 件：取值本地化） ---- */
  "capValue.supported": "支持",
  "capValue.unsupported": "不支持",
  "capValue.unknown": "未知",
  "capValue.none": "无",
  "capValue.warm": "热续接",
  "capValue.cold": "冷续接",
  "capValue.own_process": "仅本进程",
  "capValue.all": "全部",
  "capValue.protocol": "协议内审批",
  "capValue.mirror": "镜像审批",
  "capValue.unverified": "未验证",
  "capValue.immediate": "立即中断",
  "capValue.tool_boundary": "工具边界中断",
  "capValue.declared": "声明",
  "capValue.bench": "假引擎实测",
  "capValue.live": "真机实测",
  "capValue.fixed": "固定",
  "capValue.constrained": "受限",
  "capValue.open": "开放",

  /* ---- 能力清单的题名（与内核 FEATURE_PATHS 一一对应） ---- */
  "feature.structuredEvents": "结构化事件",
  "feature.sessions.list": "会话列举",
  "feature.sessions.create": "新建会话",
  "feature.sessions.resume": "会话续接",
  "feature.sessions.history": "原生历史",
  "feature.sessions.branch": "会话分支",
  "feature.card.streaming": "流式输出",
  "feature.card.tools.calls": "工具调用",
  "feature.card.tools.output": "工具输出",
  "feature.card.terminal": "终端卡",
  "feature.card.fileChanges": "文件变更",
  "feature.card.artifacts": "产物",
  "feature.card.plan": "计划",
  "feature.card.reasoning": "推理过程",
  "feature.card.permissions": "审批",
  "feature.card.questions": "提问",
  "feature.card.authentication": "认证",
  "feature.card.usage": "用量",
  "feature.card.interrupt": "中断",
  "feature.externalCli.supported": "站外 CLI",
  "feature.externalCli.resume": "CLI 续接",
  "feature.models.reasoning": "推理强度",
  "feature.models.providers": "Provider 选择",

  /* ---- 卡片 ---- */
  "card.type.reply": "回复",
  "card.type.tool": "工具调用",
  "card.type.plan": "计划",
  "card.type.terminal": "终端",
  "card.type.artifact": "产物",
  "card.type.file": "文件 · {op}",
  "card.type.question": "提问",
  "card.type.permission": "审批请求",
  "card.type.authentication": "认证请求",
  "card.type.error": "出错",
  "card.type.warn": "警告",
  "card.type.info": "提示",
  "card.type.extension": "扩展事件",
  "card.type.unknown": "未知条目 · {kind}",
  "card.reasoning.title": "思考过程",
  "card.reasoning.titleSeconds": "思考过程 · {seconds} 秒",
  "card.running": "生成中",
  "card.text.empty": "（空回复）",
  "card.text.userRole": "我",
  "card.tool.call": "调用",
  "card.tool.output": "输出",
  "card.tool.failed": "失败",
  "card.tool.progress": "进度 {value}",
  "card.error.retry": "重试",
  "card.error.showDetail": "查看详情",
  "card.error.hideDetail": "收起详情",
  "card.permission.fallbackTitle": "需要审批",
  "card.authentication.fallbackTitle": "需要认证",
  "card.answered": "已回答",
  "card.answeredWith": "已回答：{answer}",
  "card.question.fallback": "引擎在等一个回答",
  "card.question.freeText": "直接回答…",
  "card.question.submit": "提交",
  "card.usage.input": "输入 {value}",
  "card.usage.output": "输出 {value}",
  "card.usage.reasoning": "思考 {value}",
  "card.usage.total": "合计 {value}",
  "card.usage.context": "上下文 {used}/{window}",

  /* ============ batch11-shell =====     真路由 + 404 + 导航中文名 + 旧页面词典迁移（批次十一）。
     概览页那几行是**字标/印刷元素**（AD-78：其余不动），中英两列同值。 */

  /* ---- 404 / 幽灵页 ---- */
  "notFound.page.title": "没有这一页",
  "notFound.page.sub": "这个地址对不上任何一页。可能是链接过期了，也可能是拼错了。",
  "notFound.project.title": "没有这个项目",
  "notFound.project.sub": "项目树里找不到它。可能已经删掉，或者改了标识。",
  "notFound.back": "回概览",

  /* ---- 概览页 ---- */
  "home.stamp.plane": "KAUS CONTROL PLANE",
  "home.stamp.year": "EST. 2026",
  "home.vertical.left": "DESIGN · DEV · TRADING",
  "home.vertical.right": "SELF-EVOLVING · HAND-GOVERNED",
  "home.logo.sub": "X · THE APEX",
  "home.title.line1": "A SELF-GOVERNING",
  "home.title.line2": "FLEET OF AGENTS",
  "home.scale.left": "AGENTS · SKILLS · INHERITANCE",
  "home.stat.loading": "LOADING",
  "home.stat.nodes": "{count} NODES",
  "home.stat.levels": "{count} LEVELS",
  "home.stat.levelsUnknown": "— LEVELS",
  "home.stat.rootSkills": "{count} ROOT SKILLS",
  "home.stat.rootSkillsLoading": "LOADING ROOT SKILLS",
  "home.hotspot.view": "VIEW AGENT",
  "home.hotspot.label": "LATEST AGENT · {skills}",
  "home.action.newConversation": "新会话",
  "home.action.projects": "看项目",

  /* ---- 通用（旧页面迁移时补的） ---- */
  "common.cancel": "取消",
  "common.create": "创建",
  "common.save": "保存",
  "common.confirm": "确定",
  "common.more": "更多",
  "common.empty": "空",
  "ui.loading": "加载中…",
  "ui.loadFailed": "加载失败：{error}",

  /* ---- 项目树（次级导航） ---- */
  "tree.twinModes": "分身模式",
  "tree.noMatch": "没有匹配的项目",

  /* ---- 新建项目弹窗 ---- */
  "newAgent.title": "新建项目",
  "newAgent.sub": "取个名字、选好上级，工作台会建一份干净的项目。",
  "newAgent.field.display": "显示名",
  "newAgent.field.id": "项目标识",
  "newAgent.field.parent": "上级",
  "newAgent.field.desc": "描述",
  "newAgent.placeholder.display": "例如：Marketing",
  "newAgent.placeholder.id": "自动生成…",
  "newAgent.placeholder.desc": "一句话描述（可选）",
  "newAgent.noParent": "暂不设上级",
  "newAgent.parentsError": "上级列表加载失败：{error}",
  "newAgent.idInvalid": "项目标识只能用小写字母、数字和连字符，长度 1–32。",
  "newAgent.created": "已在「{parent}」下创建「{name}」",
  "newAgent.createdDraft": "已把「{name}」建成草稿",
  "newAgent.createFailed": "创建失败：{error}",

  /* ---- 移动项目弹窗 ---- */
  "reparent.title": "移动「{name}」",
  "reparent.topLevel": "顶层",
  "reparent.previewFailed": "预览失败：{error}",
  "reparent.previewing": "正在预览改动…",
  "reparent.position": "位置",
  "reparent.affects": "连带 {count} 个下级",
  "reparent.twinWarning": "它是主项目的分身。移动它会影响共享关系。",
  "reparent.configChanges": "配置变化",
  "reparent.noConfigChanges": "没有可继承的配置变化",
  "reparent.skills": "技能",
  "reparent.inheritOn": "沿树继承已开",
  "reparent.inheritOff": "继承已关",
  "reparent.compacted": "已压平",
  "reparent.footnote": "只改组织关系。会话与数据一个都不动。",
  "reparent.confirm": "确认移动",
  "reparent.moved": "已把「{name}」移到 {target}",
  "reparent.moveFailed": "移动失败：{error}",

  /* ---- 项目轮盘 ---- */
  "graph.head.title": "项目",
  "graph.head.browse": "浏览",
  "graph.head.governance": "治理",
  "graph.mode.aria": "项目视图模式",
  "graph.mode.chat": "对话",
  "graph.mode.governance": "治理",
  "graph.overlay.title": "项目 · 治理",
  "graph.drafts": "草稿（{count}）· 拖到节点上挂靠：",
  "graph.draftBadge": "草稿",
  "graph.dropTop": "拖到这里升为顶层",
  "graph.rootSub": "根项目",
  "graph.tag.twin": "TWIN",
  "graph.tag.apex": "APEX",
  "graph.tag.node": "NODE",
  "graph.openChat": "进入对话",
  "graph.details": "详情",
  "graph.section.identity": "身份",
  "graph.section.capabilities": "能力",
  "graph.section.inheritance": "继承",
  "graph.row.type": "类型",
  "graph.row.parent": "上级",
  "graph.row.model": "模型",
  "graph.row.gateway": "网关",
  "graph.row.role": "角色",
  "graph.row.parentConfig": "上级配置",
  "graph.row.skillLinks": "技能链接",
  "graph.stat.skills": "技能",
  "graph.stat.links": "链接",
  "graph.stat.children": "下级",
  "graph.value.mainAgent": "主项目",
  "graph.value.twinShared": "分身 · 与 X 共享",
  "graph.value.apexRoot": "根项目",
  "graph.value.standalone": "独立",
  "graph.role.none": "独立",
  "graph.role.source": "技能源",
  "graph.role.shared": "共享成员",
  "graph.value.topLevel": "顶层",
  "graph.value.fromParent": "继承自 {name}",
  "graph.value.localBaseline": "本地基线",
  "graph.value.disabled": "已停用",
  "graph.note.twin": "分身模式与 X 共享配置、技能和记忆，只有 SOUL 里的角色描述不同。",
  "graph.hint.details": "完整配置、守则、记忆与运行设置都在「详情」里。",
  "graph.addChild": "添加项目",
  "graph.addChildUnder": "在 {name} 下新建",

  /* ---- 仪表盘 ---- */
  "dash.title": "仪表盘 · Dashboard",
  "dash.sub": "系统健康 · 待你处理 · 最近改动，三块一眼看全。生成于 {at}。",
  "dash.health.title": "系统健康",
  "dash.health.unit": "个项目",
  "dash.health.breakdown": "{main} 主 · {twin} 分身 · {sub} 子",
  "dash.health.drafts": " · {count} 草稿",
  "dash.row.disabled": "停用",
  "dash.row.disabledValue": "{killed} 主动",
  "dash.row.disabledCascade": " · {count} 级联",
  "dash.row.hardLint": "必须修的技能问题",
  "dash.row.softLint": "建议优化的技能",
  "dash.row.mcp": "MCP 工具（主项目）",
  "dash.row.mcpCount": "{count} 个",
  "dash.row.mcpInherited": "已继承 MCP 的项目",
  "dash.row.constitution": "指令与守则",
  "dash.constitution.off": "未启用",
  "dash.constitution.on": "已为 {count} 个启用",
  "dash.review.title": "待你处理 · {count} 项",
  "dash.review.empty": "✓ 全员合规 · 无需处理",
  "dash.review.killed": "已停用（你主动停的，去恢复或确认）",
  "dash.review.hardLint": "{count} 个必须修的技能问题",
  "dash.review.draft": "草稿还没安排位置（去项目轮盘的治理模式拖到岗位）",
  "dash.changes.title": "最近变更 · {count} 条",
  "dash.changes.empty": "尚无任何变更记录",
  "dash.lint.failed": "读取技能体检明细失败：{error}",
  "dash.lint.title": "规范体检",
  "dash.lint.hard": "{count} 必须修",
  "dash.lint.soft": "{count} 建议",
  "dash.lint.clean": "✓ 全部合规",

  /* ---- 设置（原 Config 浮层） ---- */
  "config.title": "设置 · Config",
  "config.badge": "v{version} · {count} 类",
  "config.sub": "主项目（X）的全部配置，按类型归类。只读视图——改配置请编辑 {source}。敏感值（密钥 / Token）一律打码，集成栏只看是否配置、绝不读取凭据。生成于 {at}。",

  /* ---- 资料库（原 Vault 浮层） ---- */
  "vault.title": "资料库 · Vault",
  "vault.new": "新建笔记",
  "vault.missing": "资料库路径不存在：{root} —— 去 dashboard/vault_config.json 改 root。",
  "vault.sub": "全局资料库——所有项目共享同一个库（检索 / RAG 由 llmwiki 负责，这里只做浏览和读写）。",
  "vault.count": "{count} 篇",
  "vault.openObsidian": "用 Obsidian 打开",
  "vault.emptyTree": "空资料库",
  "vault.openFailed": "打开失败：{error}",
  "vault.view.md": "渲染",
  "vault.view.raw": "原文",
  "vault.view.edit": "编辑",
  "vault.saved": "已保存 {path}",
  "vault.saveFailed": "保存失败：{error}",
  "vault.new.title": "新建笔记 · 全局资料库",
  "vault.new.sub": "写到资料库根目录下的相对路径。示例：raw/inbox/2026-05-31.md 或 wiki/concepts/rag.md。",
  "vault.new.placeholder": "如：raw/inbox/new-note.md",
  "vault.new.bodyPlaceholder": "# 标题\n\n正文…",
  "vault.new.pathRequired": "路径不能为空",
  "vault.new.pathSuffix": "路径必须 .md 结尾",
  "vault.new.created": "已创建",

  /* ---- 仓库（原 Warehouse 浮层） ---- */
  "warehouse.title": "仓库 · Warehouse",
  "warehouse.badge": "{sources} 来源 · {items} 条目",
  "warehouse.sub": "离线冷存储——存放可按需装到项目的能力件（技能 / MCP），不参与日常运行。点条目卡的 + 可多选项目一次复制多份（各自独立副本，互不影响）；该项目的继承模式后代会自动继承。默认保留仓库这份（冷存档），可勾选同时移除。",
  "warehouse.search": "搜条目名 / 描述…",
  "warehouse.filter.all": "全部 {count}",
  "warehouse.filter.type": "{label} {count}",
  "warehouse.type.skill": "技能",
  "warehouse.type.mcp": "MCP",
  "warehouse.type.config": "配置",
  "warehouse.empty": "仓库为空。把「可能用得上但当下无场景」的技能 / 配置按来源放进 ~/.hermes/.warehouse/ 即可。",
  "warehouse.item.noDesc": "（无描述）",
  "warehouse.assign": "分配到项目（可多选）",
  "warehouse.picker.title": "把「{name}」复制到哪些项目？",
  "warehouse.picker.titleTyped": "把「{name}」（{type}）复制到哪些项目？",
  "warehouse.picker.subConfig": "勾选的每个项目会把它合并进各自 config.yaml 的 {key}；可多选。",
  "warehouse.picker.subSkill": "勾选的每个项目会得到一份独立副本（移入各自 skills/{source}/）；可多选，互不影响。",
  "warehouse.picker.treeFailed": "加载项目树失败：{error}",
  "warehouse.picker.treeLoading": "加载项目树…",
  "warehouse.picker.removeAfter": "复制后同时从仓库移除这份（默认保留作冷存档）",
  "warehouse.picker.selected": "已选 {count} 个",
  "warehouse.picker.copy": "复制到 {count} 个项目",
  "warehouse.picker.copying": "复制中…",
  "warehouse.removeConfirm": "复制后从仓库移除「{name}」？这会删除冷存档里的这一份。",
  "warehouse.copied": "「{name}」已复制到 {copied}/{total} 个项目",
  "warehouse.copiedSkipped": "（{count} 个跳过）",
  "warehouse.copyFailedSome": "未成功：{list}",
  "warehouse.allocFailed": "分配失败：{error}",
  "warehouse.role.main": "主",
  "warehouse.role.twin": "分身",
  "warehouse.role.source": "源",
  "warehouse.role.shared": "共享",
  "warehouse.role.standalone": "独立",
  "warehouse.node.skills": "{count} 技能",

  /* ---- 旧终端启动器 / 实验室（flag 关闭时才出现） ---- */
  "legacy.terminal.kicker": "真实终端",
  "lab.kicker": "薄终端 · V1",
  /* ---- batch11-pages ---- */
  "engines.write.saved": "已保存",
  "engine.field.save": "保存",
  "engine.field.cancel": "取消",
  "engine.field.saveFailed": "保存失败：{error}",
  "graph.addProject": "添加项目",
  "graph.addProject.under": "在 {name} 下新建",
  "card.tool.head": "工具调用 · {name}",
  "card.awaiting": "待回答",
  /* ---- batch11 merge ---- */
  "theme.toLight": "切换到浅色",
  "theme.toDark": "切换到深色",

  /* ---- batch12 ---- */
  /* 会话页版式重做（★ 定稿 G）：居中阅读栏 + 折叠工具行 + 输入框工具栏。 */
  "conversation.loading": "正在载入这段对话…",
  "conversation.jumpLatest": "回到最新 ↓",
  "conversation.replying": "{engine} 正在回复…",
  "conversation.replying.generic": "正在回复…",
  "conversation.stop": "停止",
  "conversation.stopFailed": "停止失败：{error}",
  "conversation.bar.project": "项目",
  "conversation.bar.engine": "引擎",
  "conversation.bar.model": "模型",
  "conversation.bar.approval": "审批模式",
  "conversation.model.confirm": "把默认模型改成 {model}？这会影响这个项目后续所有会话。",
  "conversation.model.failed": "切换模型失败：{error}",
  "conversation.usage.pill": "总 {value}",
  "conversation.usage.title": "输入 {input} · 输出 {output} · 合计 {total}",
  "conversation.usage.unknown": "—",
  "card.tool.group": "运行了 {count} 个工具",
  "card.tool.seconds": "{seconds}s",
  "card.tool.running": "运行中",
  "card.tool.done": "完成",
  "card.reasoning.row": "思考",
  "card.reasoning.rowSeconds": "思考了 {seconds}s",
  "card.reasoning.running": "正在思考…",
  "card.permission.allowed": "已允许",
  "card.permission.denied": "已拒绝",

  /* ---- batch13 ---- */
  /* 输入区工具栏（DESIGN ★ I）。审批模式的三档文案按 AD-106。 */
  "bar.workspace": "工作目录",
  "bar.engine": "引擎",
  "bar.model": "模型",
  "bar.reasoning": "推理强度",
  "bar.execution": "运行模式",
  "bar.approval": "审批模式",
  "bar.attach": "添加附件",
  "bar.approval.ask": "每次询问",
  "bar.approval.auto": "自动放行",
  "bar.approval.bypass": "完全访问",
  "bar.approval.readOnly": "只读",
  "bar.approval.plan": "计划模式",
  "nav.projectSettings": "项目设置",
  "draft.project.choose": "选择项目…",
  "bar.approval.deny": "全部放行",
  "bar.source.engine": "来自引擎配置",
  "bar.source.catalog": "目录默认",
  /* 五类旧卡片的行语法（DESIGN ★ G）。 */
  "card.plan.row": "计划",
  "card.plan.progress": "{done}/{total} 完成",
  "card.plan.status.pending": "待办",
  "card.plan.status.in_progress": "进行中",
  "card.plan.status.completed": "已完成",
  "card.plan.status.blocked": "受阻",
  "card.terminal.row": "终端",
  "card.file.row": "修改了 {path}",
  "card.file.group": "修改了 {count} 个文件",
  "card.artifact.row": "产物",
  "engine.approval.aria": "{name} 的审批模式",
  "card.question.answered": "已回答 · {prompt}",
  /* ---- phase4：双表面（到终端 / 回站内 / 只读态 / 校准） ---- */
  "surface.action.openExternal": "到终端打开",
  "surface.action.stopFirst": "先停止当前回合",
  "surface.action.returnToCard": "回到站内",
  "surface.external.banner": "正在外部终端运行",
  "surface.external.since": "启动于 {time}",
  "surface.composer.placeholder": "这条会话正在外部终端运行，回到站内后可继续",
  "surface.sidebar.external": "在外部终端运行",
  "surface.degraded.title": "没能自动打开终端，请自己粘这条命令",
  "surface.degraded.copy": "复制",
  "surface.degraded.copied": "已复制",
  "surface.degraded.dismiss": "知道了",
  "surface.confirm.leaseHeld": "另一处正在使用这条会话（{owner}，{acquiredAt}）。强制接管可能导致双写，确定？",
  "surface.confirm.takeover": "强制接管",
  "surface.confirm.externalActive": "终端好像还开着，强行收回可能丢失终端里正在进行的操作",
  "surface.confirm.reclaim": "强行收回",
  "surface.notice.takenOver": "{owner} 的写权已被接管（{reason}）",
  "surface.owner.unknown": "另一处",
  "surface.reason.unknown": "未说明原因",
  "surface.history.group": "外部终端期间的 {count} 条记录",
  "surface.history.incomplete": "部分记录无法恢复（{gaps}）",
  "surface.launches.label": "历史",
  "surface.launches.running": "仍在运行",
  "surface.launches.exit": "退出码 {code}",

  /* ---- batch15 ---- */
  "project.crumb.wheel": "← 轮盘",
  "bar.model.empty": "引擎未报告可用模型",
  "bar.model.none": "未设置",
  "engine.probe.offline": "引擎离线：{message}",
  "conversation.runState.engineOffline": "引擎离线",

  /* ---- batch16 ---- */
  /* 第 1 件：停止是**一个过程**。点下去先进「正在停止…」，事件到了才收敛；
     8 秒没等到确认就把按钮放开并补一句中性提示，绝不本地冒充成功。 */
  "conversation.stopping": "正在停止…",
  "conversation.stopping.unconfirmed": "引擎没有确认停止，可再试一次",
  /* 第 7 件：「回到最新」带未读计数。 */
  "conversation.jumpLatest.count": "回到最新 · {count} 条新消息",
  /* 第 4 件：消息悬停动作。 */
  "conversation.action.copy": "复制",
  "conversation.action.copied": "已复制",
  "conversation.action.resendPrevious": "重发上一条",
  "conversation.action.editResend": "编辑重发",
  "conversation.action.aria": "消息操作",
  /* 第 2 件：「回到站内」的语义说明。 */
  "surface.return.title": "回到站内",
  "surface.return.body":
    "只是回到这个页面继续看，不会终止终端里的任务；终端仍持有写权，站内输入保持只读，直到终端退出或你选择强制收回。",
  "surface.return.stay": "回到页面",
  "surface.return.force": "强制收回写权",
  "surface.return.stayed": "已回到页面查看；终端仍在运行，站内输入保持只读。",
  /* 第 5 件：菜单过滤框。 */
  "bar.filter.placeholder": "输入以过滤",
  "bar.filter.empty": "没有匹配项",
  /* 第 3 件：外壳判定完成前的中性骨架。 */
  "shell.loading": "正在载入…",
  /* 第 8 件：旧面板里硬编码的文案入典。 */
  "sidebar.pin": "钉住侧栏",
  "sidebar.unpin": "取消钉住侧栏",
  "ui.refresh": "重新载入",
  "ui.refreshing": "正在重新载入…",
  "ui.cancel": "取消",
  "ui.ok": "确定",

  /* ---- batch17 ---- */
  /* 第 1 件：引擎中途没了（后端合成的 `run.failed{code:"runtime_lost"}`）。
     一句中性解释，不是错误横幅。 */
  "conversation.runtimeLost": "上一轮因引擎中断结束",
  /* 第 2 件：展开态只有预览串时的那一行；以及回填小点的说明。 */
  "card.tool.commandLine": "命令：{value}",
  "card.tool.backfilled": "已从原生历史补齐",

  /* ---- batch18 ---- */
  /* 第 1 件：后端说这条会话有条目、重放却一条都没到。中性一行，不是错误。 */
  "conversation.historyMissing": "这条会话的历史没有随重放到达；刷新可以再试一次。",
  /* 第 2 件：能力表的「操作」列（AD-144）。 */
  "capabilities.col.actions": "操作",
  "capabilities.action.aria": "能力操作",
  "capabilities.action.block": "禁止（子树）",
  "capabilities.action.blockHere": "在本项目禁止",
  "capabilities.action.clear": "删除本层赋值",
  "capabilities.action.unblock": "解除禁止",
  "capabilities.toast.block": "已禁止 {name}（含子项目）",
  "capabilities.toast.blockHere": "已在本项目禁止 {name}（含子项目）",
  "capabilities.toast.clear": "已删除本层赋值 {name}",
  "capabilities.toast.unblock": "已解除禁止 {name}",
  "capabilities.toast.failed": "操作失败：{error}",

  /* ---- batch19 ---- */
  /* 第 1 件：登录态里 AD-93 的 `auth_model` 三档，说成人话跟在「已登录」后面。 */
  "engine.authModel.managed-credential": "用本机配置的凭据",
  "engine.authModel.own-auth": "引擎自己的登录",
  "engine.authModel.session-scoped": "每条会话各自登录",
  /* ---- batch23 ---- */
  /* 第 2 件：右下角 Group 浮窗（DESIGN ★ J）。 */
  "group.pill": "协作组",
  "group.pill.count": "协作组 · {count}",
  "group.minimize": "最小化",
  "group.empty": "暂无协作组",
  "group.noMembers": "这个组还没有成员。",
  "group.memberCount": "{count} 名成员",
  "group.new": "新建组",
  "group.new.title": "组名",
  "group.new.placeholder": "给这个组起个名字",
  "group.new.submit": "建组",
  /* 成员行与「…」菜单四动作。 */
  "group.member.actions": "成员操作",
  "group.member.pause": "暂停",
  "group.member.resume": "恢复",
  "group.member.rejoin": "重新加入",
  "group.member.remove": "移出组",
  "group.member.promote": "提升为普通会话",
  "group.member.running": "正在运行",
  "group.member.conversationGone": "这条会话已不在",
  "group.state.active": "进行中",
  "group.state.paused": "已暂停",
  "group.state.left": "已移出",
  "group.state.failed": "已失败",
  /* 「添加成员 ▾」两条路。 */
  "group.add": "添加成员",
  "group.add.existing": "选择已有会话",
  "group.add.spawn": "启动新成员",
  "group.add.pick": "选一条会话",
  "group.add.noConversations": "这个项目下还没有可加入的会话",
  "group.spawn.project": "项目",
  "group.spawn.model": "模型",
  "group.spawn.first": "第一句话",
  "group.spawn.firstPlaceholder": "第一句话（可不填）",
  "group.spawn.submit": "启动",
  /* 关闭组的处置弹窗：两个选项各写清楚后果（AD-148 ② 的三行表）。 */
  "group.close": "关闭组",
  "group.close.title": "关闭协作组",
  "group.close.sub": "「{title}」关掉之后就不能再加人、移人了；组还查得到，成员会话一条都不会被删除。",
  "group.close.keep": "保留",
  "group.close.keep.body": "组内新建的会话提升为普通会话，留在项目里；本来就长期保留的一个字段都不动；用完即弃的那几条归档。",
  "group.close.archive": "归档",
  "group.close.archive.body": "组内新建的会话与用完即弃的那几条一起归档（置为已结束，历史与原生会话都留着）；长期保留的仍然不动。",
  "group.close.counts": "本组：长期保留 {persistent} 条 · 关组时再决定 {decide} 条 · 用完即弃 {ephemeral} 条",
  "group.close.confirm": "关闭组",
  /* 第 3 件：侧栏那枚组图标；第 4 件：会话页页头那枚 chip。 */
  "group.sidebar.title": "协作组：{title}",
  "group.chip": "协作组：{title}",
  /* 写操作的回执。 */
  "group.toast.created": "已建组「{title}」",
  "group.toast.closed": "已关闭「{title}」",
  "group.toast.paused": "已暂停该成员",
  "group.toast.resumed": "已恢复该成员",
  "group.toast.removed": "已移出该成员（会话本身没有被删除）",
  "group.toast.promoted": "已提升为普通会话",
  "group.toast.memberAdded": "已加入本组",
  /* batch42：拖进来的那一条回执点名标题（拖源顺手带了）。 */
  "group.drop.noGroup": "还没有协作组——先在右下角建一个，再把会话拖进去。",
  "group.drop.pickGroup": "开着 {count} 个组，把它拖到具体那个组的成员栏上。",
  "group.toast.memberAddedNamed": "已加入 {title}",
  "group.toast.memberSpawned": "已启动新成员",
  /* batch43：把项目拖到组上 = 组里现有的「启动新成员」（路径 B）。 */
  "group.drop.projectHint": "{name} · 拖到右下角的协作组上 = 从这个项目启动一名成员",
  "group.toast.memberSpawnedFrom": "已从 {name} 启动一名成员",
  /* 409 码表的人话（`docs/ops/groups.md` §5）。按 code 分支，不按文案。 */
  "group.error.groupClosed": "这个组已经关闭了，不能再改它的成员",
  "group.error.memberDuplicate": "这条会话已经是本组成员",
  "group.error.memberElsewhere": "这条会话正在「{title}」里，一条会话同时只能待在一个组",
  "group.error.memberElsewhereUnnamed": "这条会话已经在另一个组里，一条会话同时只能待在一个组",
  "group.error.memberLeft": "这位成员已经被移出了；用「重新加入」把他请回来",
  "group.error.bindingRequired": "这个项目还没有默认引擎，先去项目页接一个",
  "group.error.bindingMismatch": "选中的引擎不属于这个项目",

  /* ---- batch25：应用到引擎（物化）与配置漂移 ----
     文案口径取自 `docs/ops/projection.md`：默认只预演、写之前先备份、凭据一个字
     不写、Drift 四态里 `unmanaged` 不算漂移。 */
  "materialize.action": "应用到引擎",
  "materialize.title": "应用到引擎：{name}",
  "materialize.sub": "把这个项目算出来的有效能力写进引擎自己的配置文件。下面是预演结果——此刻磁盘上一个字节都没动。",
  "materialize.loading": "正在预演…",
  "materialize.failed": "预演失败：{error}",
  "materialize.retry": "重试",
  /* 三段。 */
  "materialize.willWrite.count": "将写入 {count} 处",
  "materialize.willWrite.empty": "没有需要写的改动",
  "materialize.unchanged": "无变化 {count} 项",
  "materialize.unsupported": "不会写入",
  "materialize.unsupported.count": "不会写入 {count} 项",
  /* 变更行。 */
  "materialize.act.set": "写入",
  "materialize.act.unset": "清空",
  "materialize.value.none": "（空）",
  "materialize.value.expand": "展开完整值",
  "materialize.value.collapse": "收起",
  /* `unsupported.reason` 四档（ops 文档 §7）。 */
  "materialize.reason.credential_bearing": "带凭据",
  "materialize.reason.credential_bearing.note": "凭据永远不由仪表盘写入。",
  "materialize.reason.not_mapped": "这台引擎的写表里没有落点",
  "materialize.reason.not_blockable": "这条是禁止，但没有登记「关闭值」",
  "materialize.reason.factory_protected": "当前的值不是本仪表盘写的",
  // batch44：工作目录文件投影的四种拒绝理由（AD-166 / AD-167）。
  "materialize.reason.no_workspace_root":
    "这个项目没有工作目录，文件投影无处可写",
  "materialize.reason.no_workspace_root.note":
    "到项目设置里给它填一个工作目录，再回来应用。",
  "materialize.reason.workspace_shared":
    "这个工作目录已经属于另一个项目",
  "materialize.reason.workspace_shared.note":
    "两个项目共用一个工作目录时，后写的会把先写的抹掉——所以这次一个字都没写。请给其中一个项目换一个工作目录。",
  "materialize.reason.no_convention": "这台引擎没有声明项目指令写哪个文件",
  "materialize.reason.workspace_denied": "这个工作目录不能写",
  "materialize.reason.invalid_config": "这条能力没有可写的内容",
  "materialize.reason.other": "其它",
  "materialize.adopt": "接管这些键，本次一并写入",
  "materialize.adopt.note": "接管照样先备份；接管之后这些键才会出现在漂移检查里。",
  "materialize.warnings": "提醒",
  /* 写入。 */
  "materialize.confirm": "备份后写入",
  "materialize.writing": "写入中…",
  "materialize.done": "已写入",
  "materialize.done.backup": "备份：{path}",
  /* batch54（真机 PJ-04）：回滚目标此前写死成 `config.yaml`——那是物化 MCP 那会儿
     的唯一目标；批次四十四让指令投影复用了同一个弹窗，于是投影 `AGENTS.md` 之后
     它照样说 `config.yaml`，照抄那条命令会盖错文件。目标改成从备份文件名推出来的
     真实那一个（`X.kaus-backup-<时间戳>` → `X`）。推不出来就不编一个（下一条）。 */
  // batch54
  "materialize.done.rollback": "要回滚就把它拷回去：cp {path} {target}",
  "materialize.done.rollback.unknown":
    "要回滚就把这份备份拷回它原来的位置（备份文件名里读不出原文件名）。",
  "materialize.done.noBackup": "后端这次没有报告备份路径。",
  "materialize.busy": "有 {count} 条会话在运行，先停止再写",
  "materialize.writeFailed": "写入失败：{error}",
  /* 漂移。 */
  "drift.field": "配置漂移",
  "drift.count": "{count} 处被引擎侧改过",
  "drift.inSync": "与项目一致 · 检查于 {time}",
  "drift.view": "查看",
  "drift.title": "配置漂移：{name}",
  "drift.sub": "引擎侧现在的值与本仪表盘上次写下的登记对不上的那些键。没写过的键不算漂移，不列在这里。",
  "drift.expected": "期望",
  "drift.actual": "实际",
  "drift.state.drifted": "被改过",
  "drift.state.missing": "已不在",
  "drift.empty": "没有对不上的键",
  "drift.checkedAt": "检查于 {time}",
  /* ---- batch26：Group 展开态三栏 + 广播 / 定向 · 接入引擎面板 ---- */
  /* 第 1 件：三栏（窄屏折成 tab）。栏名同时是 tab 名，一份文案两处用。 */
  "group.tabs": "分栏",
  "group.tab.members": "成员",
  "group.tab.timeline": "对话",
  "group.material.materials": "资料",
  "group.material.add": "添加资料",
  "group.material.import": "导入文本文件",
  "group.material.edit": "编辑",
  "group.material.remove": "删除",
  "group.material.refresh": "刷新资料",
  "group.material.title": "标题",
  "group.material.content": "内容",
  "group.material.save": "保存",
  "group.material.cancel": "取消",
  "group.material.empty": "暂无资料",
  "group.material.source": "来源",
  "group.material.note": "笔记",
  "group.material.busy": "停止讨论后可编辑",
  "group.material.capture": "存入本组资料",
  "group.material.copy": "复制",
  "group.material.copied": "已复制",
  "group.material.fileError": "请选择 UTF-8 文本文件（TXT、MD、CSV、JSON），不超过 48 KB",
  "group.material.limit": "{used} / {max} 字符",
  "group.material.overview": "成员近况",
  "group.material.loadFailed": "资料加载失败",
  "group.material.copyFailed": "复制失败",
  "group.material.selection": "粘贴选定的内容",
  "group.tab.packet": "资料",
  /* 成员行菜单新增的第五个动作。 */
  "group.member.directed": "定向发送",
  /* 时间线：一条消息的种类与状态。 */
  "group.kind.broadcast": "广播",
  "group.kind.directed": "定向",
  "group.kind.system": "系统",
  /* batch45a：成员一轮的发言（PRD §B2）。种类小标本身不出现在气泡上——成员那一行
     的第一眼是名字，不是「这是一条 member_turn」；键留着是给 tDynamic 兜底的。 */
  "group.kind.member_turn": "成员发言",
  "group.turn.tools": "跑了 {count} 个工具",
  "group.turn.failed": "这一轮失败了",
  "group.turn.interrupted": "这一轮被打断",
  "group.turn.noText": "这一轮没有留下正文。",
  "group.turn.truncated": "正文过长，这里只留了前 4000 字",
  "group.timeline.loading": "正在取组时间线…",
  "group.timeline.empty": "这个组还没有消息。",
  "group.timeline.failed": "组时间线取不到：{error}",
  "group.timeline.pending": "发送中…",
  "group.timeline.sendFailed": "没发出去：{error}",
  /* batch45c：时间线改正序之后的两枚（★J-6）。「加载更早」在顶，「回到最新」在底。 */
  "group.timeline.loadEarlier": "加载更早",
  "group.timeline.loadingEarlier": "正在取更早的…",
  "group.timeline.loadEarlierFailed": "更早的取不到：{error}",
  "group.timeline.jumpLatest": "回到最新 · {count} 条新",
  /* 投递摘要与展开后的每成员状态。「跳过」不是错误，只有 failed 那一档染色。 */
  "group.delivery.sent": "{count} 已发",
  "group.delivery.skipped": "{count} 已跳过",
  "group.delivery.failed": "{count} 失败",
  "group.delivery.none": "没有投递记录",
  "group.delivery.status.sent": "已发",
  "group.delivery.status.skipped_paused": "已跳过（这位成员暂停中）",
  "group.delivery.status.skipped_left": "已跳过（这位成员已被移出）",
  "group.delivery.status.failed": "失败",
  /* 第 2 件：广播输入框。 */
  "group.compose.aria": "对这个组说的话",
  "group.compose.placeholder": "发送消息…",
  "group.compose.send": "发送",
  "group.compose.sending": "发送中…",
  "group.compose.closed": "这个组已经关闭，不能再发消息。",
  "group.compose.targetDirected": "定向发给「{title}」",
  "group.compose.clearTarget": "取消定向",
  /* batch52 第 2 件：这里的 `{name}` 是**后端算的显示名**（含重名后缀 `#2`），
     不再是会话标题——两条都叫 media 的会话入组之后，这枚勾选框的可访问名此前
     读出来是两个一样的「media」。 */
  // batch52
  /* batch28：勾选口径。勾了就照勾的发（含暂停成员），一个没勾中就拦住。 */
  "group.compose.targetsNone": "暂无可参与的成员",
  "group.compose.clearTargets": "清除勾选（发给全部）",
  "group.toast.sendFailed": "没发出去：{error}",
  /* 第 1 件右栏：Context Packet（只读 + 复制 + 刷新）。 */
  "group.packet.loading": "正在生成 Context Packet…",
  "group.packet.failed": "Context Packet 取不到：{error}",
  "group.packet.empty": "这个组还没有成员，Context Packet 是空的。",
  "group.packet.copy": "复制 Markdown",
  "group.packet.copied": "已复制 Context Packet",
  "group.packet.copyFailed": "复制失败：这个浏览器没给剪贴板权限",
  "group.packet.refresh": "刷新",
  "group.packet.generatedAt": "生成于 {time}",
  "group.packet.col.member": "成员",
  "group.packet.col.last": "最近一句",
  /* 第 3 件：会话页换模型被引擎顶回来（400 model_rejected）。 */
  "conversation.model.rejected": "引擎没有接受 {model}：{reason}",
  /* 第 4 件：「接入引擎…」面板（内容与 docs/ops/backends.md §4 同源）。 */
  "engines.connect": "接入引擎…",
  "engines.connect.title": "接入引擎",
  "engines.connect.search": "搜索 Agent",
  "engines.connect.refresh": "刷新",
  "engines.connect.available": "可用",
  "engines.connect.registered": "已接入",
  "engines.connect.detected": "已安装",
  "engines.connect.notDetected": "未检测到",
  "engines.connect.empty": "没有匹配的 Agent",
  "engines.connect.docs": "官方文档",
  "engines.connect.install": "安装",
  "engines.connect.connecting": "接入中…",
  "engines.connect.attached": "已添加",
  "engines.connect.addToProject": "添加到项目",
  "engines.connect.connect": "接入",
  "engines.connect.copyFailed": "复制失败：这个浏览器没给剪贴板权限",
  "engines.connect.auth": "登录",
  "engines.connect.pick": "选一个引擎",
  /* 八个预设各一句「登录怎么做」（backends.md §4 的表）。 */
  "preset.claude-code.auth": "先在终端把它的 CLI 登好；审批档经 session/set_mode 下发。",
  "preset.codex.auth": "订阅登录或 API key 二选一；用 key 时 env_keys 只写变量名。",
  "preset.opencode.auth": "先在终端登好，仪表盘不代登录。",
  "preset.gemini.auth": "ACP 面带 --experimental 前缀，形状可能随版本变。",
  // batch49：官方登记在 ACP 公共目录里的第十二行（AD-170）。
  "preset.antigravity.auth": "先在终端交互式跑一次 `agy` 登录，ACP 服务端复用缓存下来的凭据；二进制要自己下载解压，放进 PATH 或用 command 覆盖成绝对路径。",
  "preset.qwen.auth": "同样是实验期接口，先在终端登好。",
  "preset.openclaw.auth": "先在终端登好，仪表盘不代登录。",
  "preset.pi.auth": "社区适配器，装了才有；没装时探测会如实报「连不上」。",
  "preset.hermes-acp.auth": "它的第二条路；走原生 HTTP 面请改用 native-http 配置，两者别同时指同一个家目录。",
  // batch42：内核目录里的后三条（AD-158 中继真机取证）。
  "preset.dsh.auth": "官方 harness 的 ACP 面（命令 `dsh --profile acp`）：没有登录方法，凭据只经环境变量。",
  "preset.deepseek-acp.auth": "社区适配器（`npx -y deepseek-acp`）：凭据只经环境变量；0.8.x 的续接有缺陷，界面上那两个入口直接不显示。",
  "preset.kilo.auth": "用它自带的 ACP 面（`kilo acp`，不是社区的 kilo-acp 壳）：凭据只经环境变量，模式切换本版还没接。",
  /* 引擎卡上的预设小标（quirks 不显示，AD-71）。 */
  "engine.field.preset": "预设",
  /* ---- batch27：真机热修（A1 三段常显 · A4 还原 · C5 组小标 · D4 模型下拉 · 首句失败） ---- */
  /* 第 3 件：三段标题始终可见，空段也留一句话（DESIGN ★K）。 */
  "materialize.unsupported.empty": "没有被挡下的键",
  /* 第 6 件：漂移只读弹窗上的主按钮 → 切到 materialize 流程。 */
  "drift.materialize": "用项目配置覆盖引擎侧",
  /* 首句没发出去：常驻通知（不是 toast），点名是谁 + 后端的原因 + hint。 */
  "group.notice.firstMessageFailed": "「{title}」建好了，但第一句话没发出去：{error}",
  "group.notice.noReason": "引擎没有说明原因",
  "group.notice.dismiss": "知道了",
  /* 成员行上的小标：详情在时间线顶上那条通知里。 */
  "group.member.firstMessageFailed": "首句没发出去",
  /* 后端热修新增的投递原因码 → 人话（表里没有的码退回后端 detail 原文）。 */
  "group.delivery.reason.member_paused": "这位成员暂停中，这一条没有投出去",
  "group.delivery.reason.member_left": "这位成员已经不在组里",
  "group.delivery.reason.turn_already_running": "上一轮还在运行，等它结束再发",
  "group.delivery.reason.runtime_start_failed": "引擎没起来",
  "group.delivery.reason.conversation_missing": "这条会话已经不在了",
  /* ---- batch30：隧道兜底 · 勾选语义二改 · Context Packet 竞态 · provider 折叠 ---- */
  /* 第 1 件：SSE 到不了时改走轮询。中性一句，不是错误提示。 */
  "transport.polling": "连接方式：轮询",
  /* 第 2 件：勾了什么就发给什么——重置按钮把勾选放回默认（全部进行中的成员）。 */
  /* 第 4 件：折起来的 provider 组头（点开才列它的模型）。 */
  "bar.group.collapsed": "{count} 个模型",
  "bar.group.expand": "展开 {name}",
  "bar.group.collapse": "收起 {name}",

  /* ---- batch31：模型快照过期改为采纳（AD-155） ---- */
  /* 时间线上的中性一行：不是错误，也不是警告，只是说明这一刻发生了什么。 */
  "timeline.modelAdopted": "引擎当前模型是 {to}，这条会话从这里起按 {to} 继续（原快照 {from}）",
  /* 同上，但原快照缺席（旧事件 / 会话本来就没有快照）时不编一个出来。 */
  "timeline.modelAdopted.noFrom": "引擎当前模型是 {to}，这条会话从这里起按 {to} 继续",

  /* ---- batch33：ACP 登录态与目录探测（AD-157） ---- */
  /* own-auth 的引擎（凭据归它自己管）：我们查不到登录态，只能转述它自己的说法。
     括号里那半句是**责任归属**，不是修辞——不写，用户会以为仪表盘真的知道。 */
  "engine.auth.signedInSelfReported": "已登录（引擎自报）",
  /* 没登录时模型目录必然是空的：那不是「你没设」，是「现在还问不出来」。 */
  "engine.value.signInFirst": "登录后可见",

  /* ---- batch35：进程拉起失败要有人话（AD-159） ---- */
  /* 与 runtime_start_failed 分开的一行：那一条是「网关没跑」，这一条是「这台机器
     上这条命令执行不了」，修法完全不同（后端 detail.hint 会接在后面）。 */
  "group.delivery.reason.agent_spawn_failed": "引擎进程没能拉起来",

  /* ---- batch37：外部评审 R1/R8/R9 的前端修复 ---- */
  /* 第 2 件：单条消息的 Markdown 渲染塌了，只塌这一条——中性一句，不上红，
     下面照旧把原文摆出来（用户要的是内容，不是错误页）。 */
  "card.text.renderFailed": "这条消息没能渲染成格式，下面是原文",

  /* ---- batch38：旧写接口鉴权 + `kaus/user.message.failed` 的渲染（R5 / R6） ---- */
  /* 用户气泡置灰后那一行原因：前半句是我们说的「没被处理」，后半句是后端原话。 */
  "conversation.message.failed": "这句话没有被处理：{reason}",
  /* 后端没给人话时的兜底：只说事实，不编原因。 */
  "conversation.message.failed.noReason": "这句话没有被处理",
  /* 置灰气泡上的重发入口。重发 = 用同一段正文重新发一次（新的对账编号）。 */
  "conversation.message.resend": "重发",
  /* 配不上 `clientRef` 时的中性系统一行：不指认是哪一句，只说有一次发送没送到。 */
  "timeline.userMessageFailed": "有一句话没有被处理：{reason}",
  "timeline.userMessageFailed.noReason": "有一句话没有被处理",

  /* ---- batch39：输入框与「重发」在运行中的两处口径统一 ---- */
  /* 第 1 件：运行中按 Enter 之后输入框底下那一句。中性——它是"现在还轮不到"，
     不是错误；草稿留在框里，本轮结束这句话自己消失。 */
  "conversation.composer.blockedWhileRunning": "上一轮还在运行，等它结束再发（或先停止）",
  /* 第 2 件：运行中那枚禁用的「重发」的 title。只说什么时候能点，不解释为什么。 */
  "conversation.message.resend.wait": "等本轮结束",
  /* ---- batch40：信息密度收敛（DESIGN ★L） ---- */
  /* 第 2 条：导航里收着的那一组。名字要短——它自己也占一行。 */
  "nav.more": "更多",

  /* 第 1 条：原概览页三段。batch42（用户裁决：概览页取消）把「继续」「项目」
     两段删了（侧栏的复印件），`overview.recent.*` / `overview.projects.*` /
     `overview.state.*` 随之出典；剩下的「需要处理」搬到了草稿页上。 */
  "overview.attention.title": "需要处理",
  /* 两档说人话：一档是"它停在那儿等你"，一档是"上一次没发出去"。 */
  "overview.attention.awaiting": "等你处理",
  "overview.attention.failed": "上一次没发出去",

  /* 第 3 条：引擎卡默认只给两行摘要，其余收进「详情」。 */
  "engine.details.toggle": "详情",
  /* 摘要第二行的登录态：只在**没登录 / 说不清但后端给了下一步**时才出现。
     已登录不写一个字——"一切正常"不需要占一行。 */
  "engine.summary.signedOut": "未登录",

  /* 第 4 条：能力表的内容列改成人话摘要，原始 JSON 收进「展开」。 */
  "capabilities.value.expand": "展开原始值",
  "capabilities.value.collapse": "收起",
  "capabilities.showAll": "显示全部 {count} 项",
  "capabilities.showLess": "只看前 {count} 项",
  /* ---- batch41：铭牌副行（DESIGN ★M）。三个数，为零的不写。batch42 起它在草稿页上。 ---- */
  "overview.plate.attention": "{count} 条等你处理",
  "overview.plate.running": "{count} 条运行中",
  "overview.plate.projects": "{count} 个项目",

  /* ---- batch45b：房间循环（PRD §B4 / AD-168）。只有一枚新按钮：「停」。 ---- */
  /* 组头那一行只在房间**转着**的时候出现；停下来的四种结局各自在时间线上说话。 */
  "group.thread.round": "第 {round}/{cap} 轮",
  "group.thread.turnOf": "轮到 {name}",
  "group.thread.stop": "停",
  "group.thread.stopFailed": "没停下来：{error}",
  /* 收口那一条是用户等的那个答案，所以它是这一片里唯一一张高亮卡。 */
  "group.turn.final": "最终答复",
  "group.turn.round": "第 {round} 轮",
  /* 「没有新内容」是发生过的事，但不该占三行——折成一行，点开看是哪几位。 */
  "group.turn.passed": "本轮 {count} 人略过",
  /* 广播框：用户做的事是「对这个房间说一句」，不是「广播给 N 个人」。 */
  "group.compose.sendToRoom": "发到房间",

  /* ---- batch46：收口轮（PRD §B4 第二次修订）。 ----
     组头在收口时说的是「谁在收」，**不写轮数**——收口那一轮不计入 round，写
     「第 2/12 轮」会让用户以为房间还在讨论，而它已经决定停了。那一枚「停」照旧在。 */
  "group.thread.closingBy": "正在收口 · {name}",
  "group.thread.closing": "正在收口",

  /* ---- batch48：三合一在组长（PRD §A6 / §B6，第四次修订）。 ----
     「已收口 N 人」随整套表态机制一起删了：收敛不再靠每个人自己点头，靠组长读完
     全场之后点不点下一个人，而那件事在时间线上看得见（组头只说「轮到谁」）。

     组长在成员栏上是一枚小标（沿用角色/状态那一档语汇，不新造一种强调，★J-2）；
     「设为组长」在成员的「…」菜单里，与暂停 / 移出并列——它是改一个字段，不是一次
     任命仪式。 */
  "group.member.leader": "组长",
  "group.member.makeLeader": "设为组长",
  "group.member.leaderFailed": "没能换组长：{error}",
  "group.member.leaderDone": "{name} 是组长了",
  /* 输入框里打半角 @ 弹出的那枚浮层（Grok Bot 的做法，2026-09-17 截图）。
     第一项永远是「所有人」——它是一条「别按名字算」的指令，不是某几个人。 */
  "group.mention.everyone": "所有人",
  "group.mention.aria": "选一位成员",

  /* ---- batch52 第 5 件：组内改名（AD-173）。 ----
     真机上测试员找不到改名入口。侧栏 ⋯ 菜单里那条「改名」改的是**项目**的显示名
     （`labels.json`），不是会话标题，更不是这位在组里的名字。这里给的是后者：写
     成员的 `roleLabel`，它在显示名回退链第一位，而**会话自己的标题不受影响**——
     同一条会话可以在这个组里叫「评审」，在别处仍旧叫它本来的名字。
     留空 = 恢复成会话标题，所以这句提示说的是「留空」而不是「清除」。 */
  "group.member.rename": "在本组叫什么",
  "group.member.renameTitle": "「{name}」在本组叫什么",
  "group.member.renameField": "组内名字",
  "group.member.renameHint": "只改他在这个组里的名字；会话自己的标题不变。留空 = 恢复成会话标题。",
  "group.member.renameSave": "保存",
  "group.member.renameDone": "组内改名：{name}",
  "group.member.renameFailed": "没改成：{error}",

  /* ---- 草稿页与协作组补齐的词条 ---- */
  "draft.attachments.imagesOnly": "此引擎只支持图片，请选择 PNG、JPEG、WebP、GIF 或 AVIF。",
  "draft.model.notApplied": "引擎没有采纳所选模型，请重新选择后发送。",
  "draft.send.unconfirmed": "发送结果尚未确认，请进入已经建立的会话查看状态。",
  "draft.send.openExisting": "打开会话确认",
  "group.coordinator": "组长",
  "group.processing": "处理中",
  "group.runLog": "执行记录",
  "group.run.reconnecting": "连接中断，正在重试",
  "group.memberCount.one": "1 名成员",
} as const;
