# 写接口的鉴权边界（运维视角）

对应裁决：D-17 / AD-66（会话接口的两道闸）、批次三十七 R5（把同一道闸套到旧写接口上）。
出处：`docs/quality/external-review-2026-09-07.md` R5。

## 1. 两道闸，缺一不可

| 挡谁 | 怎么挡 |
|---|---|
| 被诱导访问恶意页面的浏览器 | `Origin` 白名单（本机 host + 端口）。恶意页面改不了这个头。 |
| 本机上的其它进程 | 本地 token。同机进程不带 `Origin`，只有「读得到 token 文件」才算有权限；token 文件 `0600`，等价于「和你同一个用户」。 |

顺序是**先 Origin 后 token**：跨站请求即使猜对了 token 也是 **403**，不是 401——错误码
本身不泄露「token 对不对」。

token 怎么拿：`GET /api/session-auth/bootstrap`（只对同源浏览器请求或本机非浏览器请求返回），
之后所有请求带 `Authorization: Bearer <token>`。

## 2. 闸的覆盖范围（批次三十七起）

**所有 `/api/` 下的 `POST` / `PUT` / `PATCH` / `DELETE`**，无论它是新的会话接口还是
`server.py` 里那几十条旧接口。实现是一层 middleware（`session_bootstrap.install_legacy_write_auth`），
在 `server.py` 装配时无条件安装——**与 `session_host_v1` 开关无关**：旧接口在不在闸内，
不该取决于新功能开没开。

- **读接口不动。** 概览、项目树、能力矩阵这些 `GET` 是仪表盘的正常渲染路径。
- **`?token=` 对写接口无效。** 那个例外只为 `EventSource` 开（它带不了自定义头），而 SSE 全是 `GET`；
  写接口多开一个取值来源，就多一条会被写进浏览器历史与代理日志的路。
- **`GET /api/session-auth/bootstrap` 也是无条件挂上的。** 它原本长在会话 router 上、跟着
  `DASH_FEATURE_SESSION_HOST_V1` 一起被 flag 挡掉；闸既然无条件装，发 token 的那条路就不能
  跟着一个与它无关的新功能开关一起消失，否则 flag 关着时是个死结——写接口要 token，而唯一能
  拿到 token 的路没被挂上。补挂的**只有这一条**（零内核依赖），会话 API 的其余部分照旧归 flag 管；
  flag 开着时那条路由已经在了，按路径判重不重挂，两边用的是**同一个** `SessionAuthPolicy` 对象。
- **会话 router 自己那层 dependency 照旧在。** 同一份判断跑两次是幂等的；少一层就得靠
  「middleware 永远先跑」这条约定，那不是安全该依赖的东西。
- 副作用一条：向**不存在**的 `/api/` 路径发写请求，现在先回 401/403 再谈 404。

## 3. 启动自检

`server.py` 启动时调 `session_bootstrap.unprotected_mutating_routes(app)`，列出**闸盖不住**的
写路由（判据只有一条：路径不在 `/api/` 之下）。正常情况下它是空的；不空就会在启动日志里打一行：

```
[legacy_write_auth] 以下写路由不在鉴权闸内,请把它们挪到 /api/ 下或显式加闸：POST /xxx
```

看到这行就当成事故处理——那条路由现在没有任何应用级身份屏障。仓库级用例
`tests/test_batch37_legacy_write_auth.py::test_the_real_server_has_no_unprotected_mutating_route`
把「这个列表必须为空」钉成了发布门槛。

同一段日志还有一句更严重的：

```
[legacy_write_auth] 装配失败,旧写接口目前没有闸：...
```

它意味着 token 文件建不出来（磁盘只读、权限不对），此时**所有**写接口都是裸的。

## 4. 还没被这道闸盖住的

- **WebSocket**（`/ws/chat/{name}`、`/ws/terminal-lab/{name}`）：不是 HTTP 写方法，本批没有
  纳入。AD-14 计划在 Phase 4 真机验收后连同 `/api/pty`、Terminal Lab 一起删除；在那之前它们
  仍然是一条没有闸的入口。
- **静态文件与 SPA 深链**：只读。

## 5. 自己验一遍（十分钟）

后端重启之后，在终端里跑：

```bash
# 0) 先拿 token（flag 开没开都应该是 200）
curl -s -H 'Origin: http://127.0.0.1:5174' -H 'Sec-Fetch-Site: same-origin' \
  http://127.0.0.1:8877/api/session-auth/bootstrap
# 期望 {"token":"..."}；回 404 就说明这条路由没挂上，写接口会全线 401

# 1) 跨站 Origin 的写请求 → 403，且不应有任何副作用
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Origin: https://evil.example' http://127.0.0.1:8877/api/kanban/daemon/start
# 期望 403

# 2) 本机、无 token → 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8877/api/kanban/daemon/start
# 期望 401

# 3) 读接口照旧
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8877/api/projects
# 期望 200
```

前两条如果回 200，说明**后端没重启**（launchd 的重启陷阱见 `docs/HANDOFF.md` §3），
不是这道闸没装上。

这两条的 403/401 由**全局中间件在路由匹配之前**给出，所以它们证明的是这道闸生效，
而不是这条路径存在——换任意一条写路径写法结果都一样。
