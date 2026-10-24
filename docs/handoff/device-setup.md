# 换设备继续开发

## 取得可运行源码

使用 GitHub 默认分支。目录可自行选择，不要求旧设备的用户名或固定 checkout 路径。

```sh
git clone https://github.com/haorantang97/Kaus.git
cd Kaus
git rev-parse HEAD
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cd web
# 使用 .nvmrc 指定的 Node 版本；当前是 24.15.0
npm ci
npm run check
cd ..
.venv/bin/python -m pytest -q kernel
```

不要复制旧机器的 `node_modules`、`.venv` 或托管 Agent 二进制来代替安装。原生版本/签名、平台与架构不同会影响运行；安装失败和认证缺失应分别定位。

第一次开发使用独立数据目录。`HERMES_HOME` 是兼容名称；不要把真实 Agent 主目录误当空测试目录。`DASH_DOMAIN_DB` 和 `DASH_SESSION_API_TOKEN_FILE` 可隔离 Kaus DB/令牌。电脑操作测试使用独立 `KAUS_COMPUTER_USE_HOME`，默认不启用。

这几项不隔离 Codex 等 Agent 的原生历史。真实消息测试使用 `drivers.discovery_scope.isolated_agent_spec`，按预设复用必要连接配置、重定向原生记录目录，再调用 `prepare_discovery_credentials` 保留原生登录身份（含 macOS Keychain）；退出作用域前关闭进程。命令入口 `python -m drivers.acp.testing.probe_adapter --preset <id> --prompt -- <command>` 已采用该流程。OpenClaw 使用独立 Gateway 测试脚本；正常模型目录读取只调用 `models.list`。详细验收见 [探测隔离](../product/discovery-isolation-20261024.md)。

```sh
mkdir -p .local/hermes .local/state
export HERMES_HOME="$PWD/.local/hermes"
export DASH_DOMAIN_DB="$PWD/.local/state/domain.sqlite3"
export DASH_SESSION_API_TOKEN_FILE="$PWD/.local/state/session_api.token"
export KAUS_COMPUTER_USE_HOME="$PWD/.local/computer-use"
cp dashboard-config.example.json dashboard-config.json
# 初次纯 UI 验收可把本地配置的 backends 只保留 backend:mock。
.venv/bin/python server.py
```

打开 `http://127.0.0.1:8877/new`。示例配置含原生连接说明，Mock 不调用模型；真实引擎需自行完成原生安装/登录并在 Kaus 接入。连接发现不等于已授权。浏览器热更新在 `web/` 运行 `npm run dev`，桌面和 8877 使用 `npm run build` 生成的 `web/dist`。

## macOS 桌面窗口

后端已经运行时可在另一个终端执行：

```sh
.venv/bin/python -m pip install -r desktop/requirements.txt
.venv/bin/python desktop/app.py
```

桌面壳使用当前 8877 服务。自动唤起服务使用兼容 launchd 名 `com.hermes.dashboard.backend`；新设备未安装这个服务时，先手动启动后端即可。仓库包含图标、Info.plist 和壳源码，不包含本机个人路径的 `.app` 启动器或完整签名分发安装器。

电脑操作在设置中明确开启后再安装/授权 CuaDriver。每台 Mac 重新完成辅助功能和屏幕录制授权；通过实际屏幕读取验证。不要迁移 TCC 数据库或将另一台机器的权限状态标为已完成。新系统/架构需重新验收。

## 原生终端组件与验收

DSH / DeepSeek ACP 首次打开终端时自动准备 `@dsh-tui/dsh-tui@0.1.2`，使用独立 `kaus-tui` profile 保存组件；实际运行仍保留选中连接原来的 profile、覆盖项和原生历史目录。组件为 MIT 许可，没有 pnpm 时通过 npm 缓存执行固定版本 pnpm，不替换全局包管理器。当前验证 DSH `0.1.5-rc.3`、DeepSeek ACP `0.9.0`；其他版本按实际加载接口补验。

Cursor 当前验证 `2026.09.18` 的原生存储、codec 和 MCP parser。原生升级后先验证目录布局、技能筛选、个人/项目 MCP、冷恢复和往返，再更新版本声明；不要仅调整版本哈希使检查通过。终端凭据仍由原生配置提供，模型测试临时复用已有凭据不代表已替用户设置日常连接。

使用 `scripts/live_terminal_roundtrip.py` 明确执行真实模型验收，`scripts/native_terminal_roundtrip.py` 执行真实原生程序配合本机模拟模型，`scripts/cursor_native_store_check.py` 检查原生存储与 MCP。临时原生数据作用域必须持续到全部子进程退出；普通对话保持原生存储及 Codex 桌面互通。完整结果见 [终端往返验收](../product/terminal-roundtrip-acceptance-20261024.md)。

## 私人使用数据迁移

GitHub 只解决产品代码与开发上下文迁移。保留个人对话和 Project 配置需要单独私密传输：

1. 在旧设备结束活动运行和外部终端持有，停止 Kaus 写入；使用一致性 SQLite 备份或在完全停写后复制状态及关联文件。
2. 盘点本地 `dashboard-config.json`、项目/技能分配配置、`state/` 下领域/事件/工作台数据库、附件、模块安装/数据，以及配置中指定到其他位置的目录。路径可配置，不可假设只有一个 DB。
3. 原生 Agent 目录分别按各自迁移方式处理。Kaus 对原生历史的关联不包含原件全部内容；未迁移原件就不能声称聊天材料完整迁移。原生自动清理策略沿用用户自己的设置。
4. 钥匙串、登录和 OAuth 在新设备重新确认；本机令牌、套接字、PID、控制权回执与运行时缓存不作为可继续执行的任务搬运。远程服务仍在执行时先确认停止，不能靠删除租约绕过。
5. 在新设备的独立测试数据副本验证项目数量、配置范围、历史读取、附件打开、同 Agent 恢复与跨 Agent 接续。检查工作目录、可执行文件和外部服务地址后再切换日常使用。

迁移前保留回滚副本；不要对旧设备原生目录做批量重写。本仓库已有备份工具见 `scripts/ops/README.md`，其具体覆盖以脚本和备份清单为准，不默认涵盖每个外部 Agent 的私有目录。

## 接手检查顺序

先读 [当前状态](current-state.md) 和 [产品决策](product-decisions.md)，确认 Git 提交和本地差异；跑无账号回归；用 Mock 启动；再逐个接入已安装 Agent。真实模型、系统权限与业务实战按 [验收](verification.md) 和 [待办](open-items.md) 处理。修改前确认实际服务路径/端口，避免再次启动旧演示界面。
