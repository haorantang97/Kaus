/* 扩展事件「有没有一张自己的卡」的唯一判定处（批次二十八第 4 件 / AD-71）。
 *
 * 真机现象：ACP 会话里，引擎每次都会发 `available_commands_update`、
 * `session_info_update` 这类扩展事件。它们对用户没有任何含义——时间线上因此排出
 * 一串「Extension event」空行，把真正的对话挤散了。
 *
 * AD-71 的口径是「缺能力静默不显示」，同一条精神在这里的说法是：**没有卡的扩展
 * 事件不渲染**。注意它与 AD-126 不冲突：AD-126 说的是「已经到达的事件不受能力
 * 门控」——那是"因为矩阵 unknown 就把有意义的内容藏掉"，而这里藏掉的是一条本来
 * 就没有内容可显示的通知行。事件仍然进 reducer、仍然计数、仍然在取证里看得见，
 * 只是不占版面。
 *
 * 要给某个扩展事件做一张卡时：在这里登记 `namespace/name`，再去 CardRenderer 的
 * `extension` 分支里画它。登记表是白名单——新出现的扩展事件默认不显示，而不是
 * 默认渲染成一行空标题。
 */

import {
  MODEL_ADOPTED_NAME,
  MODEL_ADOPTED_NAMESPACE,
  USER_MESSAGE_FAILED_NAME,
  USER_MESSAGE_NAME,
  USER_MESSAGE_NAMESPACE,
} from "./reducer";
import type { AnyTimelineItem } from "./reducer";

/** 有专属渲染的扩展事件（`namespace/name`）。 */
export const EXTENSION_CARDS: ReadonlySet<string> = new Set<string>([
  /* 用户自己发的那句话。正常情况下 reducer 已经把它变成 message 条目，
     根本走不到扩展分支；留在白名单里是保险——万一哪天 reducer 改了，
     用户的话也绝不该因为这条规则消失。 */
  `${USER_MESSAGE_NAMESPACE}/${USER_MESSAGE_NAME}`,
  /* batch31 / AD-155：「这条会话从这里起按引擎当前模型继续」。它必须看得见——
     整条会话的模型在这一刻变了，藏起来就成了一个没人解释的变化。 */
  `${MODEL_ADOPTED_NAMESPACE}/${MODEL_ADOPTED_NAME}`,
  /* batch38 / 批次三十七 R6：「这句话引擎没接下」。它**没有自己的卡**——正常情况下
     reducer 已经把它变成那条用户消息上的 `deliveryStatus="failed"`，走不到扩展分支。
     登记在这里是为了配不上 `clientRef` 的那一档：那时它作为扩展条目留下，必须渲染成
     一行中性系统提示，而不是按「未登记 → 不显示」被静默丢掉——一次没送到的发送
     被藏起来，界面就等于骗人说它发出去了。 */
  `${USER_MESSAGE_NAMESPACE}/${USER_MESSAGE_FAILED_NAME}`,
]);

export function hasExtensionCard(namespace: string, name: string): boolean {
  return EXTENSION_CARDS.has(`${namespace}/${name}`);
}

/** 这一条该不该整个不渲染（连包裹它的那一层都不该有）。 */
export function isHiddenTimelineItem(item: AnyTimelineItem): boolean {
  return item.kind === "extension" && !hasExtensionCard(item.namespace, item.name);
}
