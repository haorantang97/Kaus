# 电脑操作覆盖 · 2026-11-10

**谁提供工具、谁占用屏幕都不由 Kaus 决定。** 2026-11-10 起 Kaus 不再识别、挑选、补齐或安装电脑操作工具，也没有设置页和项目级开关（决定见 `product-decisions.md`「电脑操作」，删改清单见 `verification.md` 同日）。原生 Agent 的桌面 App 能操作电脑，不代表它的 CLI/ACP 暴露相同工具。

## 工具从哪里来

1. 能力库里的插件或 MCP（用户自己装的 CuaDriver、Playwright MCP、Peekaboo 等），在技能树里分给 Project，和其他能力一样送达。
2. Agent 自带的工具（Hermes、OpenClaw 内置电脑工具，Codex / Claude Code 配置里的 MCP）。按能力导入规则处理：能关的跟着树走，关不掉的是 Agent 绑定能力。

模型是否理解图像、是否会正确使用工具，仍由该模型和原生 Agent 决定。旧的 `policies/computer_use` 行不再生效。

## 谁占用屏幕

| 情形 | 谁负责 | Kaus 做什么 |
| --- | --- | --- |
| 后台操作某个窗口（不动真实鼠标、不抢焦点） | 不需要占用 | 不上锁，多个 Agent 可以并行 |
| 接管整个屏幕 | Agent 自己：接管前 `kaus_screen_occupy`，结束后 `kaus_screen_release` | 内置 MCP 给一块牌子，先来后到排队；到期（默认 10 分钟，最长 60）或 MCP 进程退出时自动放下 |
| 能加载 MCP 的会话（含组员，组员只有屏幕两项） | 照规则调用 | 内置 MCP 自动附上，标准说明里写明规则 |
| 送不到 MCP 的连接（如未启用独立执行的 Hermes）、不照做的 Agent、Kaus 之外的程序 | 无 | 不受保护 |

牌子是协作约定，不是 OS 沙箱；系统授权与终端权限各自独立。Kaus 不按回合持锁，回合结束也不替 Agent 放下牌子。

## 授权

macOS 录屏与辅助功能由用户授给实际请求它的程序（插件或 Agent 自己），Kaus 不跳转权限面板、不读授权状态。新设备在装好插件、完成授权后，用临时窗口验收「观察 → 操作 → 再观察」，再用两个对话验证：一个占用时另一个返回 `waiting`、释放后排到的那个拿到 `occupied`。不复制本机「已授权」的结论。

## 历史证据

2026-10-23 的真实模型闭环（Codex + Kaus 托管 CuaDriver + 协作组运行时：观察/点击/再观察、双成员顺序操作、排队取消、中断与恢复）验证的是已删除的托管安装与按回合上锁链路，不能作为新方式的验收。新方式至今只有协议与单元测试（`drivers/computer_use/tests/test_lease.py`、`app/tests/test_project_memory.py` 的屏幕用例），没有真实 Agent 实测。

详细契约见 [技术说明](../computer-use.md)。
