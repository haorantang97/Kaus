/* 兼容再导出：reducer 的真身搬到了 `web/src/lib/timeline/reducer.ts`。
 *
 * 批次七 c 合并两条分支时，会话页的简版 reducer 与卡片这份内核全量镜像合一，
 * 以全量那份为准。卡片组件仍按 `./timelineReducer` 引用类型，所以这里留一层
 * 再导出——新代码请直接引 `lib/timeline/reducer`。
 */

export * from "../../lib/timeline/reducer";
