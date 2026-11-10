# 电脑操作精简：删除范围

> 2026-11-10 已执行，实际删改与测试见 `docs/handoff/verification.md`「电脑操作精简」。下文为执行前的盘点，保留作记录；其中「保留」一节里的按回合上锁、原生探测与回收、终端预约随最终决定一并删除。

> 2026-11-10 最终决定（取代同日「按窗口上锁、保留代理层」）：后台操作不上锁；全屏接管由 Agent 自己调用「占用屏幕 / 释放屏幕」（放进扩为「Kaus 内置 MCP」的项目记忆 MCP，自动附到每个对话），标准说明里写明规则。Kaus 不再识别具体工具，`cua_proxy.py` / `cua_guard.py` 照原表删除，`discovery` / `policy` / `prepare_projection` 里为识别工具、计算锁资源而保留的部分也不再需要。文末问题 1（后台进程）、2（不认识的工具）、3（自带工具整轮锁）随之失效。4：项目级「浏览器 / 桌面」开关与 `native_policy` 去掉（与技能树分配重复）；5：删除自动安装，不写清理或迁移（本机没有托管安装，也没有存量用户）；6：重写范围已定（产品决定、当前状态、覆盖说明、`docs/computer-use.md`、待办、内置 MCP 说明、验收记录），不需要再确认。

依据 2026-11-10 的产品决定（`docs/handoff/product-decisions.md`「电脑操作」）：保留「同一时间只让一个 Agent 操作屏幕」；去掉自动安装 CuaDriver、权限引导、自动补齐与防重复，电脑操作改为能力库里的普通插件或 Agent 自带工具。本稿是只读盘点的结果，**尚未改动任何代码**。

## 删除

| 位置 | 内容 |
| --- | --- |
| `kernel/drivers/computer_use/installer.py` | 下载、验签、拉起 CuaDriver，请求系统授权 |
| `kernel/drivers/computer_use/permissions.py`、`peekaboo_host.py` | 跳转 macOS 权限面板、定位实际应用 |
| `kernel/drivers/computer_use/probe.py` | 兜底工具就绪检查、读系统授权状态 |
| `kernel/drivers/computer_use/cua_proxy.py`（`cua_guard.py` 待定，见问题 1） | Kaus 托管 CuaDriver 的只开放桌面代理及调用记录 |
| `kernel/app/api/computer_use.py`、根目录 `computer_use_settings.py`，`session_bootstrap.py` 中的挂载 | 设置、安装、授权接口 |
| `web/src/components/ComputerUseSettings.tsx`、`computer-use-settings.css`，`ConfigOverlay.tsx` 中的挂载 | 启用开关、安装状态、授权面板、已接入能力表（文案内联，无 i18n 键） |

## 修改

- `computer_use/settings.py`：去掉托管安装相关函数；`is_enabled` / `runtime_revision` 删除或固定。
- `computer_use/discovery.py`：删 `discover_fallbacks`；保留 `known_provider`、`_resource`、`discover_project`。
- `computer_use/policy.py`：去掉兜底来源和分层优选；保留 `Candidate`、`from_native`、`project_controls`，`effective_controls` 不再乘开关。
- `computer_use/integration.py`：`prepare_projection` 只收集需要上锁的资源，不再注入或移除任何 MCP。
- `hermes/computer_use.py`（按选中结果开关原生工具的去重逻辑）、`skill_execution/antigravity.py` 与 `openclaw/source_mcp.py`（引用自动注入）、`acp/card_runtime.py` 与 `runtime/session_host.py`（按开关版本重建会话）、`acp/external_cli.py`（兜底分支）。

## 保留

- 锁本体：`computer_use/lease.py`、`durable.py`，`integration.quarantine` / `release_quarantine` / `reconcile_resources`。
- 原生探测与回收：`hermes/computer_use_probe.py`、`computer_use_runtime.py`，`openclaw/computer_use.py` 的停止、确认、恢复链路，adapter 钩子 `probe_computer_use` 等。
- 执行层：`acp/turns._computer_prompt`、`runtime/surface_handoff`。
- 忙碌提示 `computer_in_use`。

## 必须补的

- 锁的识别范围要扩到**能力库插件里的 MCP** 和 **Agent 自带的 MCP**（`effective.native_mcp`）；现在 `discover_project` 只看项目 MCP，插件带来的电脑工具不受锁约束。
- **注意**：现在开关关闭时，项目里的电脑工具 MCP 会被移除、也不产生锁资源。只删设置界面而不改这里，会让用户自己装的 CuaDriver 直接失效，所以设置界面和 `prepare_projection` 必须一起改。

## 测试

- 删除：`computer_use/tests/` 的 installer、permissions、peekaboo_host、probe、cua_proxy、cua_guard、opt_in、mcp_roundtrip；`tests/test_computer_use_settings.py`、`kernel/app/tests/test_computer_use.py`、`openclaw/tests/test_computer_use_opt_in.py`、`web/.../ComputerUseSettings.test.tsx`。
- 重写：integration、policy、discovery、runtime_recovery、terminal、Hermes 与 OpenClaw 的电脑操作测试、antigravity_mcp、ConfigOverlay，以及 mock 了 `is_enabled` / `runtime_revision` 的 ACP 测试。
- 保留：lease、durable_resources、external_reservations、surface_resources、driver_control。
- 新增：插件 MCP、Agent 自带 MCP 能产生 `desktop:local` 锁。

## 需要项目负责人确认

1. **CuaDriver 后台进程**：它常驻在 MCP 子进程之外。去掉代理和调用记录后，对话被取消时可能还有一个点击在执行，锁就已释放。是否接受？
2. **不认识的电脑工具**：锁只认识几个固定程序（CuaDriver、Playwright、Peekaboo）。用户装的其他桌面类 MCP，是要求插件里写标注，还是默认放行（不上锁）？
3. **Agent 自带的电脑工具**：只有 Hermes、OpenClaw 有就绪探测；Codex、Claude 只靠读配置。锁在整轮对话期间持有，只要暴露了电脑工具，纯文字的一轮也会排队。是否接受？
4. **项目级开关**：每个项目的「浏览器 / 桌面：自动或关闭」和对应的原生 MCP 禁用，保留还是一起去掉？
5. **已安装的 CuaDriver**：之前托管安装在 `~/Library/Application Support/Kaus/computer-use`，清理、迁移到能力库，还是不动？会话里旧的自动注入记录需要一次性清除。
6. **文档**：`docs/handoff/computer-use-coverage.md`、`docs/computer-use.md` 的选择与设置界面两节要重写；`product-decisions.md` 中旧的电脑操作条目与 11-10 决定冲突，执行时一并更新。
