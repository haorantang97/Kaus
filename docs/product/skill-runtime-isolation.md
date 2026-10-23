# 技能运行隔离验收

更新：2026-10-15 第二轮。已验证 Hermes 私有执行目录、Codex ACP、Claude Code ACP 的启动技能加载范围。本轮补齐 Hermes Group 从冻结清单到私有目录的投射，并修复 OpenClaw 续接参数回归；持续运行期间的范围变更及其他驱动仍有下列缺口。

## 本轮结果

| 场景 | 结果及证据 |
| --- | --- |
| Hermes 母/子/兄弟项目，继承、移入、阻断、恢复 | 原生 skills_list / skill_view 的集合与项目有效配置一致 |
| Hermes 用户及工作区插件技能 | 仅已选 namespaced 技能可读；同一插件其他工具仍返回 pong / project-pong |
| Hermes 启动自动补回 bundled 技能 | 私有目录不再补回未选技能，安装源码不改写 |
| Hermes 普通与 Group 启动 | 第二轮补齐 Group 上层投射；planning/review 从原生来源生成独立清单，安装版 loader 仅返回所选技能，实际 ACP initialize 成功 |
| Codex 工作区、用户及系统技能 | 原生 skills/list 中仅所选副本 enabled；被排除项 disabled |
| Claude 所选与被排除技能 | 实际 Skill 工具能读到所选副本；列表外调用被原生白名单拒绝 |
| 停止后恢复，并移除已选技能 | 两家 ACP 均保留原生 session ID；恢复后的有效技能集合重新计算 |
| 同绑定并行会话 | 会话计划独立，Codex 启动脚本按最终策略 hash 固定；不同清单不覆盖 |
| 旧版本、未知适配器或缺失原生 loader | 预览返回 unsupported；需要受管技能范围的启动明确失败，不退回全量原生加载 |
| 受管会话转原生终端 | Codex/Claude 当前拒绝接管，检查发生在创建原生会话之前；无选择清单的原生模式保持原功能 |
| 运行中改变技能树 | Codex/Claude 使用会话计划；Hermes 同 binding 的目录共享仍可能让新投射影响旧进程，第二轮已复现，尚未验收修复 |

Hermes 链路：临时项目树 → resolver → driver → 安装版 loader / 工具 / ACP launcher。Claude 链路：实际 ACP → 捆绑 CLI → 本机合成模型服务 → 真实 Skill 工具。Codex 链路：实际 ACP → 原生 app-server → skills/list / 线程配置 → 本机 Responses 服务。不是仅检查 UI 标签或提示词。

本轮没有使用真实账户凭据。Claude 和 Codex 的模型响应由本机协议服务提供；Codex 两次调试探针因供应商路由覆盖收到外部 401，未产生成功推理，后续已固定本地路由。不得把这些测试写成“全程没有外部网络请求”。

## 实现与范围

- Hermes：受管子进程加载策略限制普通目录、插件注册/查找与 bundled 同步；保留插件其他工具和 hooks。策略或上游必要接口缺失时停止受管启动。静态插件技能进入同一 Agent 专属树，名称及来源共同参与身份。
- Codex：真实原生 loader 枚举路径，启动进程和线程配置均写入 path enablement；选中副本通过 additionalRoots 载入。启动配置和技能副本属于 Kaus，原生配置不被重写。
- Claude：使用真实支持白名单的 ACP/CLI，将所选副本装配为技能插件，传入完整 namespace 白名单；未验证的旧版不能假报支持。
- 两家原生 available_commands 菜单可能仍列出禁用技能，Kaus 当前不展示该原始菜单。实际能力由 loader/Skill 工具控制；以后接入命令菜单时必须按有效集合显示。
- 项目树只负责技能编排；这是正常运行的技能加载范围，不是文件系统、任意 Python 插件、Hook 或 MCP 的安全沙箱。文件工具权限仍由既有审批与沙箱系统管理。

## 保留边界

1. 历史 Hermes native-home 对话保留原生行为，按项私有投射仍明确 unsupported。没有自动迁移或修改用户原生目录。
2. 其他 ACP、HTTP 驱动尚未获得这套按项原生加载适配；保存树结构不等于它们已应用范围。第二轮检查 22 个 ACP 预设，仅 Codex/Claude 配有技能适配器；其余 20 个对要求投射的选择清单静默忽略，须补齐运行模式与未应用状态。不能把目录自动生成的空选择误当用户要求禁用全部。
3. 任意动态插件注册、未知私有路径、远程账户技能、嵌套独立技能及部分链接资源仍需逐项适配。已发现的静态来源与未知缓存分开处理，不因缓存存在自动启用插件。
4. 未选择的技能可能仍是用户能直接打开的本机文件；本功能不宣称禁止 Agent 在获准文件访问后读取这些文件。
5. 完整真实模型任务质量、全部 Hook/MCP 依赖和所有平台尚未验收。本轮验证原生加载、真实工具调用、会话恢复和失败路径。
6. Codex 按启动时原生清单生成路径开关；运行中在原生目录热新增技能的持续约束尚未验证，需要重新建立实例后重新枚举。不得将启动快照验证写成持续文件系统监控。
7. 历史 `skills/ancestor-skill-dirs` 迁移仍需保留原范围后升级；本机此前只读检查该记录为 0。
8. Hermes 当前私有技能目录按 binding 共用。新会话应用不同集合时，旧进程的动态技能扫描可能跟随变化；应使用按运行实例固定的技能版本，项目记忆和会话库继续独立管理。
9. 一个工作区禁用全局插件时，当前 inventory 可能从整个目录移除该插件，影响兄弟项目。需要保留全局来源并在对应项目单独记录阻断，而非丢弃全局来源。

第二轮原生验证：`output/agent-lifecycle-r2-20261015/verify_hermes_group.json`。结构性缺口的合成复现与解决方案：`output/agent-isolation-audit-20261015-r2/`；其中严格 XFAIL 表示已复现且尚未修复，不计为验收通过。

## 证据

- [Hermes 修复与真实验证](../../output/skill-runtime-isolation-20261015-hermes-fix/REPORT.md)
- ACP 原始记录：`output/skill-runtime-isolation-20261015-acp-fix/`，包含 Claude allowed/blocked、两家 resume-evidence、Codex native-wire。
- [修复前失败基线](../../output/skill-runtime-isolation-20261015-live/REPORT.md)
- [本轮产品决策与保存规则](agent-lifecycle-verification-20261015.md)

所有项目和测试内容为合成数据；用户原生配置、聊天及技能源没有被用作测试写入目标。部署验收另行核对真实记录数量、事件内容和外部 CLI lease。
