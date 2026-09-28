"""房间：名片、房间增量、以及房间循环那台状态机（PRD §B3/§B4）。

批次四十五 a 只有上半（:func:`room_header`）；四十五 b 把下半接上来——**同一个
模块**，因为它们共用同一张显示名表（:func:`resolve_display_names`）：显示名同时是
称呼与地址，``@名字`` 的解析和名片上的称呼必须出自一处，否则用户读同一条时间线会
读出两个人来。

本模块仍然是**纯的**：没有 HTTP、没有仓储、没有引擎。取数与投递在接入层
（:mod:`app.api.group_router`），所以「拼出来的那一段长什么样」「一轮走完之后
线程该是什么状态」都可以脱开数据库单测。



为什么要有这个
--------------
此前发给成员的就是用户那句话本身，**成员不知道组里有谁**——真机截图里一个成员
自己编了个对手出来，另一个反问「哪两位」。它们不是模型幻觉，是我们没告诉它。

它**不是** Context Packet 注入（AD-154 保留）。两者的区别是三条，缺一条就不成立：

1. **格式固定**：一段模板，不随组的历史增长，也不含任何成员的正文；
2. **每条都看得见**：它只加在**投递给引擎**的文本上，时间线上存的仍是用户原文，
   而那一行的 ``metadata.roomHeaderVersion`` 记着「这一次加过」；
3. **只说结构**：你是谁、组里还有谁、怎么找他们——名字与引擎，没有第四样东西。

纯函数、不碰仓储
----------------
本模块只认几个已经取好的字段（:class:`RoomMember`），因此「拼出来的那一段长
什么样」可以脱离 HTTP、脱离数据库单测。取数在接入层（:mod:`app.api.group_router`）。
"""

from __future__ import annotations

from typing import Any, NamedTuple, Sequence

from app.collaboration.models import RoomThread

#: 房间说明的版本号。落在 ``metadata.roomHeaderVersion`` 上：格式一改这个数就
#: 加一，回头看一条老消息时才知道当时成员收到的是哪一版的名片。
ROOM_HEADER_VERSION: int = 1

#: 名片与正文之间的分隔行。单独一个常量，是因为**解析**（将来 §B4 的 ``@`` 路由
#: 要知道正文从哪里开始）与**拼接**必须用同一个字符串。
ROOM_HEADER_SEPARATOR: str = "---"


class RoomMember(NamedTuple):
    """房间说明需要知道的一名成员。

    ``listed`` 与 ``display_name`` 分开两件事：**已暂停 / 已离开的成员不列入
    「组里还有」**（PRD §B3 末句），但他们仍然占着自己那个显示名——把他们从
    命名表里一起删掉，会让剩下的人的 ``#2`` 后缀在别人暂停的那一刻悄悄变号。
    """

    member_id: str
    #: ``role_label`` > 会话标题 > 成员 id 尾段（见 :func:`resolve_display_names`）。
    raw_name: str
    #: 引擎（``backend:<key>`` 里 ``<key>`` 那一段）。取不到就是 ``None``——
    #: 那时括号整个不出现，而不是写「（未知）」（AD-71 / N §13.1）。
    engine: str | None
    #: 进不进「组里还有」那一串。
    listed: bool


def member_id_tail(member_id: str) -> str:
    """成员 id 的尾段：``member:3f2a…`` → ``3f2a8c11``。

    最后一档兜底名。取前 8 位而不是整个 uuid：它出现在一句给人（和给模型）读的
    话里，36 个字符的 uuid 会把那句话撑得没法读，而 8 位已经足够在一个成员个位数
    的组里互相区分。
    """
    tail = member_id.rsplit(":", 1)[-1]
    return tail[:8] if tail else member_id


def resolve_display_names(members: Sequence[RoomMember]) -> dict[str, str]:
    """定出每个成员**最终**的显示名：重名的第二个起加 ``#2`` / ``#3``。

    为什么必须是一张表而不是各算各的：显示名同时是**称呼**与**地址**——§B4 的
    ``@<名字>`` 要解析回成员 id，而两个都叫「写手」的成员会让那次解析在两个人之间
    抛硬币。后缀按入参顺序给（成员行本身按加入时间排），所以同一个组连着看两次，
    谁是 ``#2`` 不会换人。
    """
    seen: dict[str, int] = {}
    resolved: dict[str, str] = {}
    for member in members:
        base = (member.raw_name or "").strip() or member_id_tail(member.member_id)
        count = seen.get(base, 0) + 1
        seen[base] = count
        resolved[member.member_id] = base if count == 1 else f"{base}#{count}"
    return resolved


def _with_engine(name: str, engine: str | None) -> str:
    return f"{name}（{engine}）" if engine else name


def _leader_line(names: dict[str, str], leader_member_id: str | None) -> str:
    """名片句后面那一句「组长：X。」（PRD §B6，批次四十八）。

    组长不在这张表里（还没定、或者刚离开）就**整句不出现**，不写「组长：（无）」
    ——AD-71 的同一条。成员因此永远读不到一个指不到人的名字。
    """
    name = names.get(leader_member_id or "")
    return f"组长：{name}。" if name else ""


def room_header(
    *,
    group_title: str,
    members: Sequence[RoomMember],
    recipient_member_id: str,
    leader_member_id: str | None = None,
) -> str | None:
    """拼出房间说明那一段（**不含**正文与分隔行）。

    收件人不在 ``members`` 里 → 回 ``None``：那说明调用方手上的成员表和收件人
    不是同一次取的，宁可不加名片也不要拼出一句「你是『』」。

    组里只有收件人一个人时**不写**「组里还有：」，也不写那句 ``@`` 的用法——
    一串空的名字与一条找不到人的指令都是没有内容的话（AD-71）。
    """
    names = resolve_display_names(members)
    if recipient_member_id not in names:
        return None
    by_id = {member.member_id: member for member in members}
    recipient = by_id[recipient_member_id]
    others = [
        member
        for member in members
        if member.member_id != recipient_member_id and member.listed
    ]
    head = (
        f"[协作组「{group_title}」] "
        f"你是「{names[recipient_member_id]}」（{recipient.engine}）。"
        if recipient.engine
        else f"[协作组「{group_title}」] 你是「{names[recipient_member_id]}」。"
    )
    leader = _leader_line(names, leader_member_id)
    if not others:
        return f"{head}组里目前只有你一个人。{leader}"
    roster = "、".join(
        _with_engine(names[member.member_id], member.engine) for member in others
    )
    return (
        f"{head}组里还有：{roster}。{leader}"
        "要对某位成员说话，在回复里写 @<名字>。"
    )


# --------------------------------------------------------------------------- #
# 房间循环（PRD §B4；批次四十五 b 起，四十八第四次修订）
# --------------------------------------------------------------------------- #

#: 「我这一轮没有新内容」的三种写法。**去空白后全等**才算——一段以「（略过）」
#: 开头、后面还说了三句话的回复是有内容的，把它折叠掉等于丢掉成员说的话。
PASS_TOKENS: frozenset[str] = frozenset({"（略过）", "(略过)", "[pass]"})

#: 「我要收口了」的两种写法，判的是**开头**（后面跟着的就是给用户的答案）。
#:
#: 批次四十八起**只认组长写的**（PRD §B4 终止 2；Semantic Kernel 的
#: ``agents=[reviewer]`` 白名单同形）：判开头这件事仍在 :func:`is_final` 里，判
#: 「他是不是组长」在接入层——别人写这四个字只记一笔 ``finalIgnored``，房间照转。
FINAL_PREFIXES: tuple[str, ...] = ("最终答复", "【最终答复】")

#: 房间增量里用户那几条的署名。成员的署名是它自己的显示名。
USER_LABEL: str = "用户"

#: 系统行的署名（成员进出、轮数到顶那一句）。
SYSTEM_LABEL: str = "系统"

#: 每条房间增量末尾那几条约定（PRD §B4「投递内容」，2026-09-19 第五次修订）。
#:
#: **这一版把「点下一位」从每个人身上也拿走了。** 第四次修订留了一句「想让谁接着说
#: 就写 @名字」，真机（`docs/quality/reports/2026-09-19-group/REPORT.md` §3 ②③④）
#: 里三场讨论的评审与打杂都把它读成了传话筒：每一轮结尾 `@下一位`，于是队列永远非
#: 空，终止 3「组长不点人 → 停」一次都到不了。裁决 B：**下一轮队列只认组长的 `@`**，
#: 所以约定这一段也不能再教成员点人——教了就等于教它们写一句不生效的话，而一句不
#: 生效的话比没有这句话更坏（成员以为自己安排了顺序）。
#:
#: 现在它说四件事：顺序归组长、有请求写在正文里、没话说怎么说、组长在最后。
#: **不再需要 ``{leader}``**：举手那句从「写 @组长的显示名」改成「在正文里写明」，
#: 于是这段文本与「组长叫什么」无关了（第四次修订那条注释说的「照抄 `@组长` 等于
#: 给一条注定失效的指令」由此自然消失）。
ROOM_CONVENTIONS: str = (
    "约定：发言顺序由组长安排，不用在结尾 @ 下一位。"
    "要让谁回应你、或你还想再说一次，在正文里写明（可以写 @名字），"
    "组长会看到并决定下一轮谁说。"
    "没有新内容就只回「（略过）」。组长每轮最后发言。"
)

#: 没有组长时那一版约定（PRD §B4 终止 3/6）。
#:
#: 只剩「略过」一条：没有组长 = 下一轮队列恒空（:func:`next_round_queue` 只读组长
#: 那条，没有组长就没有那条）= 房间**一轮即停**。再教一句 `@名字` 是教一条永远不会
#: 被读到的话——这一版里成员的点名本来就不进队列，没有组长时连「组长会看到」都不
#: 成立。
ROOM_CONVENTIONS_NO_LEADER: str = "约定：没有新内容就只回「（略过）」。"

#: 投给**组长**的那一份在约定之后多出来的一段（PRD §B6 第三条，**逐字**）。
#:
#: 它必须每轮重申：组长这一轮做的三件事（点人 / 收口 / 给结论）全靠这一句说清楚，
#: 而房间没有别的地方告诉它「你是那个要收口的人」。
#:
#: 第五次修订加进来的两句各防一件真机上见过的事：「**只有你的 @ 算数**」告诉它别人
#: 结尾那些 `@下一位` 不是路由（它读全场，一定看得见）；「**别为了礼貌点人**」防的
#: 是三场里那条「点一个人接着说」的惯性——它是房间**停不下来**的那一半原因。
#:
#: 第六次修订（批次五十一）再加最后一句：**它这一段话里只有写了 `@` 的名字才路由**
#: （:func:`next_round_queue` 用 ``explicit_only=True`` 读它）。不说这一句的话，组长
#: 这一边的规则与它看到的行为就对不上——它会以为「我只是提了一句打杂」而房间把打杂
#: 拉进了下一轮（真机 §8）。
LEADER_NOTE: str = (
    "你是组长，本轮最后发言。读完全场后："
    "还有分歧、没答完的点、或有人请求回应 → 写 @名字 点下一轮该说的人"
    "（只有你的 @ 算数，可以 @所有人）；"
    "已经收敛 → 以「最终答复」开头给结论，或谁也不点（你这一段就是结论）。"
    "别为了礼貌点人。"
    "只有写了 @ 的名字才算点名，正文里提到的名字不算。"
)

#: 到达安全上限时单独投给组长的那一条（PRD §B4 终止 4，**逐字**）。
#:
#: **只有这一条路径还用它**：前三版里「全体可收口」「单人守卫」也走收口轮，这一版
#: 都没有了——组长读完全场后不点人，它那一段本身就是结论，不必再多花一次引擎。
CLOSING_INSTRUCTION: str = (
    "到达安全上限（{rounds} 轮），到此为止，"
    "请把上面的讨论收成用户要的东西：『{user_text}』，只给结论。"
)


def closing_prompt(user_text: str, *, round_cap: int) -> str:
    """触顶那一次收口的正文（房间增量拼在它前面）。

    带上**开启这条线程的用户原话**而不是一句「请总结」：用户要的可能是一个结论、
    一份清单、一段代码，组长得知道自己在收成什么。原话取不到时那一行留空引号，
    不编一句「用户没说」——它是引用，引用不到就不引用（AD-71）。
    """
    return CLOSING_INSTRUCTION.format(
        rounds=round_cap, user_text=(user_text or "").strip()
    )


#: 停止词（PRD §B10-1）。**全等匹配**，不做包含。
#:
#: 真机截图里用户在输入框打了「停止」，房间把它当成一条消息投给了成员，成员回
#: 「已停止。」——而房间其实还在转。这不是猜意图：它是一个**封闭的短词表**，且任何
#: 长于该词的句子都不命中，所以「不要停下来继续讨论」照旧是一条普通消息。
STOP_WORDS: frozenset[str] = frozenset(
    {"停", "停止", "stop", "Stop", "STOP", "暂停一下"}
)


def is_stop_command(text: str) -> bool:
    """这一句是不是「停」（PRD §B10-1）。去空白后**全等**于词表里的一个才算。

    刻意**不** casefold：``stop`` / ``Stop`` / ``STOP`` 三种写法各自在表里列着，
    而一个 ``sToP`` 更像是打错字而不是一句指令——把大小写全折起来，等于把这张表从
    「这几个词」偷偷扩成「这个词的所有写法」，而词表封闭正是这条规则敢做的理由。
    """
    return (text or "").strip() in STOP_WORDS


def is_pass(text: str) -> bool:
    """这一轮是不是「略过」（PRD §B4）。"""
    return (text or "").strip() in PASS_TOKENS


def is_final(text: str) -> bool:
    """这一轮是不是「最终答复」**开头**（PRD §B4 终止 2）。

    判开头而不是判包含：一段正文中间引用了「最终答复」四个字（「你别急着给最终
    答复」）不是收口，而按包含判会让房间在这句话上停下来。

    **它不判「谁写的」**：只认组长这一条在接入层（``author_member_id ==
    leader_member_id``）。分开是因为这个函数是纯的文本判定，而「谁是组长」是组的
    状态——把两件事揉进来，就得给一个纯函数喂一个组。
    """
    return (text or "").strip().startswith(FINAL_PREFIXES)


def resolve_mentions(
    text: str,
    members: Sequence[RoomMember],
    *,
    explicit_only: bool = False,
) -> tuple[str, ...]:
    """从一段话里解析出**点了谁的名**，返回成员 id（按入参顺序，不重复）。

    两种写法都认（PRD §A5 / §B4）：``@写手`` 与裸的 ``写手``。裸名也认，是因为产品方
    那句话是「A 和 B 你们观点冲突了，辩论一下」——用户不会为了让房间转起来而去学
    一个符号。

    **按名字长度从长到短匹配，命中就把那一段挖掉**：重名成员的显示名是
    ``写手`` 与 ``写手#2``，短的那个是长的那个的前缀，不挖掉的话一句 ``@写手#2``
    会同时点中两个人——而这正是 :func:`resolve_display_names` 那张共用表想防的事。

    **谁在说话决定用哪把尺子**（批次五十一，2026-09-20 产品方裁决）
    ------------------------------------------------------------
    ``explicit_only=True`` 时候选只剩 ``@名字``，**裸名一个都不认**。这把窄尺子
    只给 :func:`next_round_queue` 读组长那条时用；用户的开场话与
    ``metadata.mentions`` 那笔取证照旧用宽尺子（默认 ``False``，行为一个字没变）。

    为什么要分两把：第二次外派（``docs/quality/reports/2026-09-19-group-2/
    REPORT.md`` §8）里组长那句 ``@评审 请回应打杂最后的追问``，解析出来是
    ``[评审, 打杂]``——**打杂是被裸名匹配捎进去的**，组长只 ``@`` 了评审，「打杂
    最后的追问」是在谈论他。批次五十之后路由只在组长手里，而主持人必然要在话里提到
    成员（「评审的质疑成立」「打杂说的那个边界已经清楚了」），宽尺子会把这些人全塞
    进下一轮——**组长谈论谁就等于点名谁**。用户那句仍认裸名，是因为那是给人的方便，
    他不必为了让房间转起来去学一个符号；组长是在执行一条路由指令，写一个 ``@`` 是
    它本来就被 :data:`LEADER_NOTE` 教过的事。
    """
    names = resolve_display_names(members)
    if not text:
        return ()
    haystack = text
    hit: set[str] = set()
    # 长名优先：`写手#2` 必须先于 `写手` 被匹配掉。
    for member_id, name in sorted(
        names.items(), key=lambda item: len(item[1]), reverse=True
    ):
        if not name:
            continue
        candidates = (f"@{name}",) if explicit_only else (f"@{name}", name)
        for candidate in candidates:
            index = haystack.find(candidate)
            if index < 0:
                continue
            hit.add(member_id)
            # 挖掉这一段（换成一个不会再被匹配的空格），短名不再重复命中同一处。
            haystack = (
                haystack[:index] + " " * len(candidate) + haystack[index + len(candidate) :]
            )
            break
    # 回成员表的顺序（= 加入顺序），而不是它们在句子里出现的顺序：队列的次序
    # 由「谁先进组」定（PRD §A3），否则同一句话换个语序就换一个发言顺序。
    return tuple(member.member_id for member in members if member.member_id in hit)


#: 「全体」那两种写法（PRD §A5 / §B4，批次四十八）。**半角 ``@`` + 逐字**。
EVERYONE_TOKENS: tuple[str, ...] = ("@所有人", "@everyone")


def resolve_everyone(text: str) -> bool:
    """这句话里有没有点「所有人」（PRD §B4「谁该说」）。

    刻意**放在** :func:`resolve_mentions` **外面**：那个函数把一句话翻成一串成员
    id，而「所有人」不是某几个人，它是一条「别按名字算，全都算」的指令——塞进同一个
    函数就得让它返回「全体」这种第二种取值，而调用方十有八九会把它当成普通的一串。

    只认这两种写法，且要带 ``@``：裸的「所有人」在中文里太常出现（「所有人都同意」
    「我问过所有人了」），认它等于把一句叙述读成一条指令。这与
    :data:`STOP_WORDS` 是同一条口径——词表封闭才敢做精确匹配。
    """
    return any(token in (text or "") for token in EVERYONE_TOKENS)


class RoomEntry(NamedTuple):
    """房间增量里的一行。**只能由组时间线上已公开的条目构造**（AD-154）。

    ``author_member_id`` 为空时用 ``label`` 署名（用户 / 系统）。正文就是时间线上
    那一行的正文——成员各自会话里的私有过程（工具入参、中途的思考）从来不在
    时间线上，因此也进不了这里。
    """

    author_member_id: str | None
    text: str
    label: str = USER_LABEL


#: 能进房间增量的时间线 ``kind``。**这是一张白名单**：
#: ``context_packet`` / ``writeback`` 不在里面，将来多一种 kind 也默认不进——
#: 「哪些东西会被送进别人的上下文」不该靠记得改一处黑名单来保证。
PUBLIC_DELTA_KINDS: frozenset[str] = frozenset(
    {"message", "broadcast", "directed", "member_turn", "system"}
)


def public_entries(rows: Sequence[Any], *, since_sequence: int | None) -> tuple[RoomEntry, ...]:
    """时间线的行 → 房间增量的行（PRD §B4：``since_sequence`` **之后**的公开条目）。

    ``rows`` 只按属性取（``sequence`` / ``kind`` / ``author_member_id`` /
    ``content`` / ``metadata``），所以这个函数可以拿一串最小假对象单测——而「投给
    成员的那一段里到底有没有私有正文」正是最该能脱离数据库验证的一件事。

    略过的那一轮**不进增量**：它在界面上折叠，在别人的上下文里也不该占一行——
    「B 说：（略过）」是一句没有内容的话（AD-71 的同一条）。
    """
    entries: list[RoomEntry] = []
    for row in sorted(rows, key=lambda item: item.sequence):
        if since_sequence is not None and row.sequence <= since_sequence:
            continue
        if row.kind not in PUBLIC_DELTA_KINDS:
            continue
        if row.metadata.get("passed"):
            continue
        text = row.content.strip()
        if not text:
            continue
        kind = row.kind
        entries.append(
            RoomEntry(
                author_member_id=row.author_member_id,
                text=text,
                label=SYSTEM_LABEL if kind == "system" else USER_LABEL,
            )
        )
    return tuple(entries)


def room_delta(
    *,
    group_title: str,
    members: Sequence[RoomMember],
    recipient_member_id: str,
    speaker_ids: Sequence[str],
    spectator_ids: Sequence[str],
    entries: Sequence[RoomEntry],
    leader_member_id: str | None = None,
) -> str | None:
    """拼出一次房间投递的**说明段**（PRD §B4 的格式，不含分隔行与正文）。

    它是 :func:`room_header` 的扩版：名片那一句照旧（你是谁、组里有谁），后面多了
    「本轮发言人 / 旁观」、「自你上次发言以来房间里的对话」与三条约定。

    **纯函数，且只吃 ``entries``**：私有正文进不来不是靠这里判断，而是靠
    :func:`public_entries` 那张白名单——两层分开，是因为「什么算公开」是取数那一层
    的知识，而「话怎么说」是这一层的。

    收件人不在 ``members`` 里 → ``None``（同 :func:`room_header`）。
    """
    names = resolve_display_names(members)
    if recipient_member_id not in names:
        return None
    by_id = {member.member_id: member for member in members}
    recipient = by_id[recipient_member_id]
    head = (
        f"[协作组「{group_title}」] 你是「{names[recipient_member_id]}」"
        f"（{recipient.engine}）。"
        if recipient.engine
        else f"[协作组「{group_title}」] 你是「{names[recipient_member_id]}」。"
    )
    roster = "、".join(
        (
            f"{names[member.member_id]}（你）"
            if member.member_id == recipient_member_id
            else _with_engine(names[member.member_id], member.engine)
        )
        for member in members
        if member.listed or member.member_id == recipient_member_id
    )
    lines = [f"{head}组里：{roster}。{_leader_line(names, leader_member_id)}"]
    speakers = "、".join(names[mid] for mid in speaker_ids if mid in names)
    spectators = "、".join(names[mid] for mid in spectator_ids if mid in names)
    if speakers:
        line = f"本轮发言人：{speakers}。"
        if spectators:
            # 「谁在旁听」要说出来：C 后面被叫来总结时，A 与 B 得知道他一直在听。
            line = f"本轮发言人：{speakers}；旁观：{spectators}。"
        lines.append(line)
    if entries:
        lines.append("自你上次发言以来房间里的对话：")
        for entry in entries:
            who = (
                names.get(entry.author_member_id, entry.label)
                if entry.author_member_id
                else entry.label
            )
            lines.append(f"- {who}：{entry.text}")
    leader_name = names.get(leader_member_id or "")
    # 第五次修订之后这两段都是常量（不再往里填组长的显示名）。仍旧分两份：有组长时
    # 「组长会看到并决定下一轮谁说」是真的，没有组长时那句指不到人，而房间那时一轮
    # 就停——说一句「等组长安排」只会让成员等一个不存在的人。
    lines.append(ROOM_CONVENTIONS if leader_name else ROOM_CONVENTIONS_NO_LEADER)
    if leader_member_id and recipient_member_id == leader_member_id:
        # 投给组长的那一份多一段（PRD §B6）。**按收件人判**，所以组长在同一轮里
        # 收到的与别人不是同一份文本——它要做的三件事没有别的地方说得出来。
        lines.append(LEADER_NOTE)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 线程状态机（PRD §B4「终止」那一节）
# --------------------------------------------------------------------------- #


class MemberTurn(NamedTuple):
    """这一轮里某位成员说的那一段（:func:`next_round_queue` 的入参）。

    只有两格：**谁说的**与**说了什么**。下一轮该谁说，答案全在这两格里——这正是
    审计 §7.2 那条结论的形状：不问每个人「你说完了没有」，只读他们**说的话**。

    第五次修订之后 ``author_member_id`` 不再只是记账：:func:`next_round_queue` 靠它
    把组长那一条挑出来，别人那几条只是路过。
    """

    author_member_id: str
    text: str


def next_round_queue(
    turns: Sequence[MemberTurn],
    members: Sequence[RoomMember],
    leader_id: str | None,
    active_ids: Sequence[str],
) -> tuple[str, ...]:
    """本轮的发言 → **下一轮的队列**（PRD §B4「谁该说」，2026-09-19 第五次修订）。

    **组长点谁，谁下一轮说。** 只读 ``author_member_id == leader_id`` 的那一条
    （本轮应当恰好有一条：组长是每一轮的最后一位）；**成员那几条一个字都不读**。
    规则四条：

    1. 取组长本轮那条 ``member_turn``，:func:`resolve_mentions`
       （``explicit_only=True``，**只认 ``@名字``**）的命中按**它写名字的次序**去重
       ——没有那条（组长略过 / 投递失败 / 组里没有组长）→ 没有人被点名；
    2. ``@所有人`` / ``@everyone``（:func:`resolve_everyone`）命中 → **全体 active**，
       按加入顺序；
    3. 剔除已经不 ``active`` 的（暂停 / 离开），以及**组长**自己；
    4. 组长 ``active`` 时**追加到末尾**——它是每一轮的最后一位，说话时已经读完这一轮
       所有人的话（SK 那句 "max_rounds is odd, so that the writer gets the last
       round" 的代码化）。

    **为什么成员的点名不算数**（裁决 B，2026-09-19）：批次四十八真机第一份记录
    （``docs/quality/reports/2026-09-19-group/REPORT.md`` §3 ②③④）里，三场讨论的
    评审与打杂**每一轮结尾都 `@下一位`**——把「想让谁接着说就写 @名字」读成了传话
    筒。第四次修订的「三者同权」于是让队列永远非空，**终止 3 一次都没到过**，三场
    全靠组长写「最终答复」才停。把路由收回一个人手里是结构上堵死它，而不是再写一句
    约定求模型听话。成员想让谁回应，写在正文里：组长读全场，一定看得见。

    **为什么组长这条只认显式 ``@``**（批次五十一，2026-09-20 裁决）：第二次外派
    （``docs/quality/reports/2026-09-19-group-2/REPORT.md`` §8）里组长那句
    ``@评审 请回应打杂最后的追问`` 被裸名匹配捎上了打杂。路由收回一个人手里之后，
    「组长谈论谁」与「组长点名谁」就必须分开——主持人不可能不提人名，而宽尺子下它
    每提一个人就多拉一个人进下一轮。所以这一处（**只有这一处**）用
    ``explicit_only=True``；用户的开场话与 ``metadata.mentions`` 那笔取证照旧用宽
    尺子，谁在说话决定用哪把尺子。

    顺序按组长写名字的次序：这一层回答的是「组长打算让谁接着说」。
    :func:`resolve_mentions` 内部那次按加入顺序的归一是**一句话之内**的事，
    而这一版的入参本来就只有一句话。

    回空（连组长都没有）= 没有人可以说下一句，见 :func:`advance_thread` 的终止 3/6。
    """
    active = tuple(active_ids)
    active_set = set(active)
    picked: list[str] = []
    # 组长那条**只应有一条**。真出现两条（重放补记之类）时按时间线顺序都读一遍，
    # 而不是只认第一条：多读一条组长自己写的名字，不会把传话筒放回来。
    for turn in turns:
        if leader_id is None or turn.author_member_id != leader_id:
            continue
        if resolve_everyone(turn.text):
            for member_id in active:
                if member_id not in picked:
                    picked.append(member_id)
            continue
        # 组长这条**只认 `@名字`**（批次五十一）：它作为主持人必然在正文里提到
        # 成员，裸名匹配会把这些人全塞进下一轮（真机 §8）。
        for member_id in resolve_mentions(turn.text, members, explicit_only=True):
            if member_id not in picked:
                picked.append(member_id)
    queue = [
        member_id
        for member_id in picked
        if member_id in active_set and member_id != leader_id
    ]
    if leader_id is not None and leader_id in active_set:
        queue.append(leader_id)
    return tuple(queue)


def start_thread(
    *,
    previous: RoomThread | None,
    speaker_queue: Sequence[str],
    spectators: Sequence[str],
    started_by_message_id: str | None,
    leader_member_id: str | None = None,
) -> RoomThread:
    """用户说了一句 → 开一条新线程（``epoch += 1``，第 1 轮，等队首那位）。

    **组长永远排在末尾**（PRD §A3 / §B4）：不管用户点没点它，先从队列里把它摘掉，
    再追加回去。摘了再加不是绕路——用户完全可能在句子中间写它的名字（「@组长 你和
    A 讨论一下」），那时它既要在队列里，又必须是最后一位。

    ``leader_member_id`` 只在组长 ``active`` 时传（接入层判）：暂停 / 离开的组长
    不该占一个永远投不出去的队尾。

    队列空（组里一个活跃成员都没有）时直接是 ``idle``：没有人可等，标 ``running``
    只会让组头永远显示「轮到（空）」。
    """
    epoch = (previous.epoch if previous is not None else 0) + 1
    queue = tuple(mid for mid in speaker_queue if mid != leader_member_id)
    if leader_member_id is not None:
        queue = queue + (leader_member_id,)
    return RoomThread(
        epoch=epoch,
        round=1 if queue else 0,
        status="running" if queue else "idle",
        speaker_queue=queue,
        spectators=tuple(mid for mid in spectators if mid != leader_member_id),
        speaker_index=0,
        awaiting_member_id=queue[0] if queue else None,
        started_by_message_id=started_by_message_id,
    )


def stop_thread(thread: RoomThread | None) -> RoomThread | None:
    """「停」：立刻置 ``stopped``、不再等任何人。

    **正在跑的那位不叫停**（PRD §B4 / L5）：他这一轮的回复照样会被记进时间线
    （旁观者认的是 ``run.completed``，不是线程状态），只是记完之后没有下一位了。
    去把引擎打断反而会丢掉一段已经付过钱的正文。

    **收口轮也停得掉**：那一枚「停」在组头上一直是同一枚，用户按下去的意思是
    「别再往下转了」——房间凭什么因为自己正在收口就不听。
    """
    if thread is None or not thread.is_awaiting:
        return thread
    return thread.evolve(status="stopped", awaiting_member_id=None)


def advance_thread(
    thread: RoomThread,
    *,
    member_id: str,
    passed: bool,
    final: bool,
    outcome: str | None = None,
    next_queue: Sequence[str] = (),
    active_member_ids: Sequence[str] = (),
    leader_member_id: str | None = None,
    round_cap: int,
) -> RoomThread:
    """某位说完了 → 算出线程的下一个状态（**纯函数**，不投递、不落库）。

    终止判据按 PRD §B4「终止」（2026-09-18 第四次修订）的优先级。第 1 条（用户按
    「停」）在 :func:`stop_thread` 里，第 7 条（用户新消息）在 :func:`start_thread`
    里；这个函数管的是一轮走完时的那几条：

    0. **收口那一轮回来了** → ``final``（跑失败就 ``stopped``，**不重试**）；
    2. ``final`` → ``final``（终止 2）。**调用方已经判过「他是不是组长」**：非组长
       的「最终答复」不生效，那一行只记一笔 ``finalIgnored``，房间照转；
    5. 一轮走完且**没有一个人说出话**（全略过 / 全投递失败，含组长也略过）→
       ``idle``。它排在终止 3 前面：谁也没说话时确实也「没有人被点名」，而 PRD
       终止 5 专门写了一句「组长也略过 → 同此」，那就是这一格上的裁定；
    3. 一轮走完且 ``next_queue`` **除组长外为空** → 组长读完全场之后没再点任何人，
       它这一段就是结论：``final``。没有组长可言（它刚暂停 / 离开且无人接手）→
       ``idle`` + ``leader_missing``（终止 6）；
    4. 一轮走完且 ``round + 1 > round_cap`` → **安全阀**：``phase="closing"``，单独
       投给组长一条收口指令。没有组长 → 同样 ``idle`` + ``leader_missing``；
    7. 否则进下一轮，队列换成 ``next_queue``，从它的第一位重新开始。

    队列**不再在一轮之内变化**（第四次修订删掉了 ``_relay``）：所有点名只决定下一轮，
    而第五次修订之后**只有组长的点名算数**（:func:`next_round_queue`）。理由在 PRD 里
    写得很清楚——组长排在最后，本轮插队会让被插的人说在组长前面又说在组长后面，而
    「组长读完全场再说」正是这一版全部的支点。

    ``next_queue`` 由 :func:`next_round_queue` 算好传进来（接入层读本轮那几条
    ``member_turn``）。分成两个函数是因为它们回答的是两个问题：**谁接着说**是队列的
    事，**还说不说**是这里的事；而后者必须是一处，否则「房间为什么停了」就得读一遍
    路由才答得出。
    """
    if thread.phase == "closing":
        # 收口那一轮不计入 round，也不推队列：它只有两种结局。
        if outcome:
            return thread.evolve(
                status="stopped",
                awaiting_member_id=None,
                ended_reason="closing_failed",
            )
        return thread.evolve(
            status="final", awaiting_member_id=None, ended_reason="closed"
        )

    spoke = thread.spoke_in_round + 1
    passed_count = thread.passed_in_round + (1 if passed else 0)
    if outcome and member_id == leader_member_id:
        return thread.evolve(
            status="stopped",
            awaiting_member_id=None,
            spoke_in_round=spoke,
            passed_in_round=passed_count,
            ended_reason="leader_failed",
        )
    if final and not outcome:
        return thread.evolve(
            status="final",
            awaiting_member_id=None,
            spoke_in_round=spoke,
            passed_in_round=passed_count,
            ended_reason="leader_final",
        )

    index = thread.speaker_index + 1
    if index < len(thread.speaker_queue):
        return thread.evolve(
            speaker_index=index,
            awaiting_member_id=thread.speaker_queue[index],
            spoke_in_round=spoke,
            passed_in_round=passed_count,
        )

    # --- 这一轮走完了 ------------------------------------------------------- #
    counted: dict[str, Any] = {
        "spoke_in_round": spoke,
        "passed_in_round": passed_count,
    }
    stopped: dict[str, Any] = {**counted, "awaiting_member_id": None}
    if spoke and passed_count >= spoke:
        return thread.evolve(**stopped, status="idle", ended_reason="silent")

    queue = tuple(next_queue)
    if not tuple(mid for mid in queue if mid != leader_member_id):
        # 终止 3：下一轮除组长外没有人——组长读完全场之后谁也没点。它这一轮那一段
        # 就是结论（接入层把那条 `member_turn` 标成最终答复卡）。
        if leader_member_id is None or leader_member_id not in queue:
            return thread.evolve(
                **stopped, status="idle", ended_reason="leader_missing"
            )
        return thread.evolve(**stopped, status="final", ended_reason="leader_closed")
    if thread.round + 1 > round_cap:
        # 终止 4：安全阀。不硬切——单独投给组长一条「收成用户要的东西」。
        if leader_member_id is None:
            return thread.evolve(
                **stopped, status="idle", ended_reason="leader_missing"
            )
        return thread.evolve(
            **counted,
            awaiting_member_id=leader_member_id,
            status="closing",
            phase="closing",
            ended_reason="round_cap",
        )
    return thread.evolve(
        status="running",
        round=thread.round + 1,
        speaker_queue=queue,
        # 旁观 = 还活跃、但不在这一轮队列里的那些。每轮重算而不是只做减法：上一轮
        # 说过话、这一轮没被点到的那位就此转成旁观者，房间说明里那行名册才是真的。
        spectators=tuple(
            mid for mid in active_member_ids if mid not in queue
        ),
        speaker_index=0,
        awaiting_member_id=queue[0],
        spoke_in_round=0,
        passed_in_round=0,
    )


def with_room_header(header: str | None, text: str) -> str:
    """名片 + 分隔行 + 正文。``header`` 为空时**原样返回**，一个字都不加。"""
    if not header:
        return text
    return f"{header}\n{ROOM_HEADER_SEPARATOR}\n{text}"


#: 名片的固定开头。:func:`strip_room_header` 靠它认人——只有 ``---`` 是不够的，
#: 用户自己的正文里完全可能有一行分隔线。
ROOM_HEADER_PREFIX: str = "[协作组「"


def strip_room_header(text: str) -> str:
    """把名片从一段**已投递**的文本前面摘掉，取回用户原文。

    为什么会需要它：成员那条会话的 ``kaus/user.message`` 记的是**真的发出去的
    那一份**（带名片）——这是对的，不该为了好看而记一句引擎没收到的话。但
    Context Packet 的「最后一条用户消息」是给**人**看的一句摘要，让它每一条都
    从「[协作组「…」] 你是…」开头，等于把摘要的前 60 字让给一段固定模板。

    认不出名片就**原样返回**：这个函数只做减法，且只在两条证据齐全时才动手
    （开头是 :data:`ROOM_HEADER_PREFIX`，且有一行独占的分隔行）。

    批次四十五 b 放宽了一条：名片**不再必须只有一行**。房间增量（:func:`room_delta`）
    是好几行——名片、本轮发言人、房间里发生了什么、三条约定。原来那条「多于一行
    就不切」的守卫因此会把整段增量留在摘要里，而它本来就是为了防同一件事：用户自己
    正文里的 ``---`` 不该被当成分隔行。防住这件事的其实是**开头那个前缀**，所以
    留前缀这一条就够了，切的是**第一个**独占分隔行之前的全部。
    """
    if not text.startswith(ROOM_HEADER_PREFIX):
        return text
    marker = f"\n{ROOM_HEADER_SEPARATOR}\n"
    _head, separator, body = text.partition(marker)
    if not separator:
        return text
    return body


__all__ = [
    "CLOSING_INSTRUCTION",
    "EVERYONE_TOKENS",
    "FINAL_PREFIXES",
    "LEADER_NOTE",
    "PASS_TOKENS",
    "PUBLIC_DELTA_KINDS",
    "ROOM_CONVENTIONS",
    "ROOM_CONVENTIONS_NO_LEADER",
    "ROOM_HEADER_PREFIX",
    "ROOM_HEADER_SEPARATOR",
    "ROOM_HEADER_VERSION",
    "STOP_WORDS",
    "SYSTEM_LABEL",
    "USER_LABEL",
    "MemberTurn",
    "RoomEntry",
    "RoomMember",
    "advance_thread",
    "closing_prompt",
    "is_final",
    "is_pass",
    "is_stop_command",
    "member_id_tail",
    "next_round_queue",
    "public_entries",
    "resolve_display_names",
    "resolve_everyone",
    "resolve_mentions",
    "room_delta",
    "room_header",
    "start_thread",
    "stop_thread",
    "strip_room_header",
    "with_room_header",
]
