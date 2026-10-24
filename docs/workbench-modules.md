# 工作台模块接入

Kaus 提供模块容器和共用的 Agent、模型及任务接口。工作台初始为空；应用由用户独立开发、打包并安装。常用模块可拖入侧边栏。模块自己的页面、业务流程和数据结构由模块决定。

## 安装一个模块

选择“工作台 → 添加模块”，使用系统目录选择器选中应用。已有有效 `kaus-module.json` 的构建包直接进入配置；普通本地项目进入 Agent 适配流程。选择现有 Project、Agent、模型和使用权限，不需要重复录入模型密钥。纯静态模块无需选择 Agent；本地后端单独确认启用。取消固定不会删除应用和数据。

```json
{
  "id": "my-module",
  "name": "我的应用",
  "version": "1.0.0",
  "entry": "index.html",
  "permissions": ["agent:run", "data:read", "data:write"]
}
```

包内使用相对路径，模块 ID 唯一。安装复制构建产物，不依赖原开发目录。安装器拒绝符号链接、路径穿越及超限文件。更新保留已授予的交集权限；本地后端更新需要重新启用。

## 调用共用能力

将 `sdk/workbench/client.js` 随界面打包：

```js
import { connectKaus } from './client.js';
const kaus = connectKaus('my-module');
const context = await kaus.context();
const job = await kaus.jobs.start('请整理这份材料……', crypto.randomUUID());
const latest = await kaus.jobs.get(job.id);
await kaus.data.set('draft', { text: '草稿' });
```

任务使用用户为该模块选择的 Project、Agent、模型，不能通过请求覆盖。模型调用复用 SessionHost、项目配置、引擎连接和原有审批。模块不接收主应用令牌或模型密钥。

同一次提交使用同一个 idempotencyKey；请求超时后先查询任务再决定重试。切换页面不会中止任务，重启后的未完成任务由用户继续，不自动重放。模块内部任务不进入普通聊天列表。

公共方法：

| 方法 | 参数 / 用途 |
| --- | --- |
| context.get | 当前模块的绑定、授权和界面主题 |
| jobs.start | prompt、idempotencyKey |
| jobs.list / get | 列表；或 jobId 获取状态与输出 |
| jobs.cancel / resume | jobId，停止或继续未完成任务 |
| data.get / set | key；保存时另带 value，限本模块 JSON 数据 |
| workflow.get / save / run | 独立模块可选用的顺序任务原语 |
| backend.request | method、path、body，调用模块自己的本地后端 |
| ui.configure | 打开宿主配置弹窗 |
| ui.confirm | message，返回 confirmed |
| clipboard.writeText | text，返回 copied |
| navigation.openExternal | url，用户确认后打开 HTTP(S) 链接 |
| navigation.openConversation | conversationId，仅限本模块任务 |
| editor.chatcut | 专用固定剪辑视图，须声明并授予 editor:chatcut 且宿主支持；open/url、layout/bounds/visible、hide 或 status |

数据、任务与固定顺序存入独立的 workbench.sqlite3；共享的会话事件使用原有存储。

固定 ChatCut 视图在可选 Chromium 开发壳可用；`context.get` 的 `surfaces.chatcut` 表示实际支持且已获授权。普通浏览器或当前 WKWebView 壳不会宣称支持。远端视图与模块 iframe 分离，切换模块隐藏并保留剪辑进程，宿主弹窗优先；布局只能位于模块矩形内。模块可调用 `request('editor.chatcut', {action:'open', url})`，短时登录地址只放内存，作品关联只保存无凭据的稳定项目 URL。详情与实测范围见 [固定剪辑模块验收](product/chatcut-fixed-editor-acceptance-20261024.md)。

## 自有本地后端

需要后端的应用可以额外声明：

```json
{
  "backend": { "runtime": "node", "entry": "backend/main.mjs" },
  "permissions": ["backend:run", "agent:run", "data:read", "data:write"],
  "executionPolicy": "context-only"
}
```

用户启用本地后端后，Kaus 按需启动独立 Node.js 进程。后端是受信任的本机程序，具有当前系统用户的文件权限；页面 iframe 的隔离并不代表后端也被文件系统沙箱隔离。安装后端应来自用户信任的项目。

后端通过双向 JSON Lines 收发消息，日志写 stderr：

```js
// Host -> 模块后端
{ "id": "h:1", "method": "request", "params": { "method": "GET", "path": "/api/items" } }
// 模块后端 -> Host
{ "id": "m:1", "method": "jobs.start", "params": { "prompt": "任务", "idempotencyKey": "run-1" } }
// 任一方向的答复
{ "id": "m:1", "result": { "id": "任务ID" } }
```

错误答复使用 `{id,error:{code,message,status}}`。读取循环必须同时处理请求与答复，不能在等宿主答复时阻塞 stdin。后端可用的宿主方法为 context.get、jobs.*、data.*，均绑定当前模块。项目、Agent、模型或授权变化会撤销旧进程。停止应用后不自动重放有副作用的业务请求。

`KAUS_MODULE_DATA_DIR` 提供独立、稳定的数据目录；重新安装模块仍可复用。宿主不继承模型 API key 或主 token 到子进程环境。应用自己的外部服务配置由应用管理。OAuth 可使用 context.get 返回的 callbackUrl；回调不会启动后端，应用必须验证自己的 state。

`executionPolicy: project` 使用项目执行范围；`context-only` 要求引擎关闭原生工具，只处理提供的上下文。后者目前有 Hermes、Claude Code 的适配，其他引擎需完成同等验证后开放。策略由清单声明、宿主强制，并保留在任务快照中。

## 接入样本

- `examples/workbench-note`：可直接安装的纯前端便笺，演示保存、Agent 调用和停止。
- `examples/workflow-module`：独立构建的自动化样本，清单声明 capabilities: ["workflow"]；步骤编辑器和默认流程都在模块内，Kaus 不预设业务界面。通用执行器支持 {{input}}、{{previous}}、顺序执行、失败中止、取消和显式继续。
- 现有创作工作台保留在其独立项目，通过相同清单和后端协议安装。作品、Lab305 工具及写作规则属于该应用，不写入 Kaus 核心。

这些样本不会自动安装到新用户的工作台。

## 页面与进程边界

模块页面在无同源权限的 iframe 中运行，不能直接读主窗口、令牌或任意本地文件。通过单独 MessageChannel 访问宿主能力；页面导航撤销通道，重新打开才建立新连接。表单交互可以触发页面自己的处理函数，CSP 阻止直接网络提交。外部浏览器、复制和确认通过宿主交互接口完成。

当前接入包支持静态前端及可选 Node.js 后端。其他语言或既有服务可以由模块自己的 Node 适配入口连接；普通工程可以通过已接入的 Agent 生成这种接入包，是否成功取决于项目依赖与实际构建结果。


## 普通项目适配

识别只读取用户选中的目录与少量清单，区分已有模块、静态页面、前端工程、Node、Python 和未知项目；不会扫描电脑、安装依赖或运行项目脚本。适配由用户明确发起，使用选择的 Project、Agent 和模型，通过现有 SessionHost 执行。

每次尝试建立独立工作副本，包含 `project/`、`bundle/` 和 SDK/协议参考。副本不包含 `.env`、常见凭据文件、私钥、数据库、聊天日志、`.git`、`node_modules`、虚拟环境或符号链接；文本文件也检查常见硬编码密钥。复制最多 8,000 个文件、128 MiB，单个文件不超过 16 MiB。凭据识别是有限规则，不构成任意秘密识别保证；依赖应在副本内通过原生审批安装。

驱动必须支持独立工作目录投影：实际进程目录、Agent 会话目录与客户端文件根目录指向副本，同时保留项目技能、MCP 和原有权限档位。不能满足该能力的 Agent 在启动前明确返回未适配。工作目录覆盖不是操作系统沙箱；原生工具仍受其自身的权限机制控制，Kaus 不自动放宽权限。原项目的源码/配置元数据、选定正文哈希与敏感文件本地哈希在前后核验；依赖缓存和版本库只检查目录本身元数据。变化会使适配失败，不进行自动回滚覆盖。

适配任务要求保留原应用界面、交互和业务，实际构建产物必须通过清单、入口、相对资源、包大小、凭据/开发文件和 Node 后端语法检查。静态应用另外比较原页面可识别的界面文本，拒绝明显占位替换。`ready` 表示已生成通过这些检查的模块包，不是对任意业务语义完全等价的自动证明；复杂业务仍需在生成后的模块中预览和实际使用验证。

安装时，模块包、所选配置和用户勾选的权限一次发布；不会在安装与配置之间暴露临时权限。后端执行权限默认不勾选。已有模块更新也重新确认后端执行，检查失败保持旧版本。未授权模型能力时，可安装普通静态应用，无需选择 Agent。

任务持久保存状态，切换页面后可返回；等待工具审批、登录或提问时进入现有任务会话处理。任务内部会话不进入普通聊天列表。取消会停止运行中的 Agent；失败可在新副本重试，并保留用户补充要求；宿主重启将未完成任务标记为中断，等待明确重试，不自动重复执行。界面仅返回应用名、配置与必要状态，不直接显示原始路径或运行日志。

Python 项目可以被识别并交由 Agent 适配；这不代表宿主包含原生 Python 模块运行时。生成包仍需要静态前端与可选 Node 入口，或由该入口连接已有服务。缺少可用依赖、不可移植的桌面框架或外部服务时，适配会停在可处理的失败状态，不伪造替代页面。

未启动或已结束的接入任务可从列表隐藏；运行中的任务应先取消。隐藏只影响接入列表，不删除原项目、工作副本、任务记录或已安装模块。
