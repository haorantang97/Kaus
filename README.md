# Kaus

Let’s make something sick

一个在本机运行的多 Agent 工作台。把不同引擎的对话、项目配置和协作组放在同一个界面中。

## 功能

- **流式对话**：文本、工具调用、审批、提问、附件和文件预览；支持断线重连与事件回放。
- **多引擎接入**：Hermes HTTP 网关、ACP，以及部分引擎的专用桥接。模型、推理强度和权限按引擎实际能力显示。
- **协作组**：独立组长、点名、成员协作和组内资料。普通对话与组长共用引擎适配。
- **项目配置**：工作目录、指令、技能和 MCP 配置的管理与继承。
- **终端衔接**：在支持的引擎上恢复原生会话，可选择 macOS Terminal、cmux 或 iTerm2。
- **本地界面**：中英文、明暗主题、会话归档与恢复。

项目持续开发中。引擎出现在接入目录中，表示有适配入口；可用功能仍取决于原生 CLI、登录状态和模型提供商。各引擎需要自行安装和登录，本仓库不包含模型凭据或原生 Agent 二进制。

## 本地运行

主要开发环境为 macOS。需要 Python 3.11 和 Node.js 24.15 或更新的兼容版本。终端启动功能依赖 macOS；其他系统尚未完成整体验收。

```bash
git clone https://github.com/haorantang97/Kaus.git
cd Kaus

python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

cd web
npm ci
npm run build
cd ..

cp dashboard-config.example.json dashboard-config.json
.venv/bin/python server.py
```

打开 <http://127.0.0.1:8877/new>。示例配置包含无需模型的 Mock 引擎，可先检查界面；真实对话需在设置中接入已安装、已登录的引擎。完整配置见 [引擎接入](docs/ops/backends.md)。

默认兼容目录为 `~/.hermes`。希望使用独立数据目录时，在启动前设置：

```bash
export HERMES_HOME="$PWD/.local/hermes"
mkdir -p "$HERMES_HOME"
```

Kaus 自己的数据库、附件与会话令牌保存在仓库下的 `state/`，已排除在 Git 之外。接入已有 Agent 时，其登录信息和原生会话仍由该引擎管理。

## 开发与验证

后端运行后，在另一个终端启动前端热更新：

```bash
cd web
npm run dev
```

开发界面为 <http://127.0.0.1:5174>。前端通过代理访问本机 8877 端口。

```bash
# 前端边界检查、类型检查、测试、构建
cd web
npm run check
cd ..

# 内核测试：临时数据和协议模拟，不需要模型凭据
.venv/bin/python -m pip install -r requirements-dev.txt
cd kernel
../.venv/bin/python -m pytest -q
```

真实引擎验收另见 `scripts/acp_smoke.py`、`scripts/hermes_http_smoke.py` 与 `scripts/probe_openclaw.py`。模拟测试通过不代表所有提供商的真实调用均已验证。

## 代码结构

| 路径 | 内容 |
| --- | --- |
| `web/` | React、TypeScript、Vite 前端 |
| `kernel/app/` | 项目、会话、协作组、存储与 API |
| `kernel/runtime/` | 会话运行时、事件流、审批和生命周期 |
| `kernel/drivers/` | 引擎适配与协议桥接 |
| `server.py`、`*_bootstrap.py` | 本地服务与组装入口 |
| `desktop/` | 可选 macOS 窗口壳；浏览器运行不依赖它 |
| `mcp_servers/` | 本地管理能力的 MCP 接口 |
| `docs/` | 产品约定、架构和运维说明 |

[协作组](docs/ops/groups.md) · [组内资料](docs/product/group-materials.md) · [引擎与组长适配](docs/product/shared-agent-execution.md) · [安全边界](docs/ops/security.md)

## 使用边界

- 服务默认只监听 `127.0.0.1`，面向本机使用。不要直接暴露到公网。
- 模型可能读写工作目录或执行命令；审批与权限以当前引擎能力为准。运行模式与权限是不同设置。
- 首次公开版本仍包含部分历史兼容代码，旧设计文档中的阶段计划应结合当前实现阅读。

## 许可证

Kaus 自身代码尚未指定开源许可证。

第三方组件沿用各自许可证，见 [第三方声明](web/public/THIRD_PARTY_NOTICES.txt)、字体目录中的许可证和 [xterm 声明](static/vendor/LICENSE-xterm.txt)。
