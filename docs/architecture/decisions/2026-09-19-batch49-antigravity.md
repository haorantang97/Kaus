# 裁决记录：批次四十九 Antigravity 预设（2026-09-19，AD-170）

**背景**：Google 把 Antigravity 的官方 ACP 服务端登记进了 ACP 公共目录（`agentclientprotocol/registry` 仓库的 `antigravity-acp/agent.json`，v1.1.1，作者 `Google LLC`，许可 `proprietary`）。产品方 2026-09-19 就「这算不算绕过它自己的 IDE」当场裁决。

**验收**：kernel **1812 / 79 skipped**（基线 1796 / 73，多出来的 16 条是契约按目录参数化的那一整套在新行上再跑一遍，外加 `test_presets.py` 两条；多出来的 6 条 skip 同理），仓库级 246，前端 562，`npm run check` 全绿。**真机未验**——这一行一个字都没有真机依据，见下。

---

- **AD-170 Kaus 的定位是「启动器」：一个 ACP 客户端，与 Zed / JetBrains 同列。接一个官方自己登记进 ACP 公共目录的服务端，不视为第三方绕行。**

  这是**产品方的裁决**，不是工程判断。记在这里是因为它决定了目录里能出现什么：

  - **判据是「谁把它登记进目录的」，不是「它是不是官方 IDE 的一部分」。** `antigravity-acp/agent.json` 的 `authors` 是 `Google LLC`——把一个 ACP 服务端登记进公共目录，本身就是「它是给 ACP 客户端接的」这句话的官方形式。我们做的事与 Zed 打开同一份清单做的事一模一样：读 `distribution.binary[平台].cmd`，按 ACP 说话。
  - **许可是 `proprietary`（条款 <https://antigravity.google/terms>），条款风险由产品方承担。** 目录里那一行的备注逐字写着这句与裁决日期——将来有人问「为什么表里有一家 proprietary 的」，答案在表里，不在谁的记忆里。
  - **它不改变 AD-151 那条分工。** 新增一个 ACP 引擎 = 预设表加一行，Driver 一个字不改。这一批改的文件里没有 `server.py`，也没有任何一个 Driver 源文件——`test_purity.py` 那条「预设 id 不许出现在本包其余任何文件里」照旧守着。

  ### 这一行怎么填的（每个值的出处）

  - **`command=("agy_acp_server.par",)`。** 清单登记的是**平台二进制**，不是 npm 包：macOS arm64 下载 `…/releases/macos/agy-acp-server-agy_acp_server_1.1.1-darwin-arm64.zip`，解压后 `cmd` 是 `./agy_acp_server.par`。这个文件**没有固定安装位置**，所以预设只给文件名——用户把它放进 PATH，或者在 `backends[].command` 里覆盖成绝对路径（覆盖这条路 `session_bootstrap.py` 早就支持，与 AD-159 那条「绕开坏 npx」用的是同一个口子）。
  - **Linux 清单里那个 `args: ["--uid="]` 不加。** 它只出现在 `linux-x86_64` 那一条，`darwin-aarch64` 没有，含义我们没取证过。把它写进 `command`，macOS 上就会多一个引擎不认识的空值参数；把它写成「按平台分支」，目录就从一张表变成一段逻辑。两种都不做：Linux 用户自己写全 `command`，备注里说明白。
  - **`login_command="agy"`，`auth_method_ids=()`。** 官方 headless 文档说的是「复用缓存凭据，先交互式跑一次 `agy`」——所以登录这件事只能指向终端（AD-93 own-auth 照旧，`authenticate` 我们一次都不调）。`authMethods` 没取证过，空元组的含义是「没拿到」，不是「它没有」。
  - **`instructions_file=None`。** 官方文档**没写**这家在工作目录里读哪个文件。按 AD-167 / AD-71：不知道就当作不支持，界面上没有这个入口——而不是猜一个文件名写进用户的仓库，再在界面上说「已应用」。（它的权限配置在 `~/.gemini/antigravity-cli/settings.json`，那是家目录不是工作目录，两件事。）
  - **怪癖照 `gemini` 那一行的保守档**（不认 `session/resume`、需要客户端 fs、没有可切的档），`verified_bits=frozenset()`、`known_bad={}`。**整行停在 `declared`**：没有真机、没有握手记录、没有一位是测出来的。空集合读作「还不知道」，不是「已知不行」；`initialize` 里它自己说了的位连上就会覆盖回来（AD-151 的三层优先级一个字没动）。

  ### 没做的

  - **真机取证做不了**（容器里没有这个二进制，也不该为它去下载一个 proprietary 的包）。所以能力矩阵上这一行**每一格都是 `declared`**，测试单 L18 里留了一句「有 Antigravity 的话也放一位」，等真有人装了再补。
  - **不碰 `server.py`、不改别的预设。**
