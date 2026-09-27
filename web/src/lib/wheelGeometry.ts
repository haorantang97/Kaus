/* 轮盘几何：一份**解析式**的节点位置公式，供 AgentGraph 每帧算连线端点用。
 *
 * 背景（AD-81 / 批次七 d 第 2 条）：上一轮为了不在动画帧里布局抖动，把 SVG 连线改成
 * 「停下来才画」，用户一眼就看出连线滞后。正确解法不是不画，而是**画但不量 DOM**：
 * 节点的 transform 完全由 `state.current` 决定，端点因此可以解析地算出来，动画帧里
 * 零 `getBoundingClientRect`。
 *
 * 公式与 `AgentGraph` 写进 `style.transform` 的那串必须逐字对应：
 *   transform: translateY(calc(-50% + Ypx)) translateX(Xpx) rotateX(Rdeg) scale(S)
 *   transform-origin: left center;  容器 .ag-wheel-col 有 perspective: 900px
 *
 * 为什么端点可以不管 rotateX 与 perspective：连线接的是节点的**左/右中点**，
 * 它们正好落在 rotateX 的旋转轴上（局部 y = 0），旋转后 z 仍为 0，透视除法的分母
 * 是 1。所以端点只受 translate 与 scale 影响。（节点的**外接矩形**会因为上下两角
 * 转出平面而变宽 —— 这正是过去用 `getBoundingClientRect().left/right` 量端点时，
 * 连线会微微脱离节点边缘的原因。）
 */

export interface WheelNodeTransform {
  /** 与前排的距离（index − state.current） */
  offset: number;
  /** 视觉层级 0..4 */
  depth: number;
  scale: number;
  opacity: number;
  /** translateY 的像素部分（不含 -50%） */
  y: number;
  /** translateX 的像素部分 */
  x: number;
  /** rotateX 的角度 */
  rotate: number;
}

export function wheelNodeTransform(offset: number): WheelNodeTransform {
  const rank = Math.abs(offset);
  const depth = Math.min(rank, 4);
  const scale = Math.max(0.46, 1.18 - depth * 0.19);
  /* 透明度地板 0.72（批次十一第 6 件，原来是 0.16）。
     节点名 `.ag-nm` 是 88% 墨色，乘上元素透明度就是它落到画布上的实际浓度：
     0.16 时对比度只有 1.4:1，深浅两套主题都读不出来。0.72 × 0.88 ≈ 0.63，
     浅色 4.7:1、深色 6.3:1，两边都过 WCAG AA。
     深度仍然表达得出来——scale（1.18→0.46）、rotateX 与 translateX 三样都没动，
     只是不再靠"淡到看不见"来表示远。阻力常数与 AD-81 无关，一个没碰。 */
  const opacity = Math.max(0.72, 1 - depth * 0.22);
  const y = offset * 58;
  const x = -depth * 9;
  const rotate = Math.max(-18, Math.min(18, -offset * 7));
  return { offset, depth, scale, opacity, y, x, rotate };
}

/** 写进 `style.transform` 的字符串（AgentGraph 与测试共用，避免两处漂移）。 */
export function wheelTransformCss(t: WheelNodeTransform): string {
  return `translateY(calc(-50% + ${t.y.toFixed(1)}px)) translateX(${t.x.toFixed(1)}px) rotateX(${t.rotate.toFixed(2)}deg) scale(${t.scale.toFixed(3)})`;
}

export interface WheelAnchorInput {
  /** 列的左边缘，坐标系 = `.ag-cols` 的 border box */
  columnLeft: number;
  /** 列的垂直中心（节点 `top: 50%` 的那条线），同一坐标系 */
  columnCenterY: number;
  /** 节点未经 transform 的布局宽度（`offsetWidth`） */
  width: number;
  /** index − state.current */
  offset: number;
  side: "l" | "r";
}

/** 连线端点。与旧 `cen()` 一样，右端外推 6px、左端内收 2px（视觉不变）。 */
export function wheelAnchor({ columnLeft, columnCenterY, width, offset, side }: WheelAnchorInput): { x: number; y: number } {
  const t = wheelNodeTransform(offset);
  const localX = side === "r" ? t.scale * width : 0;
  return {
    x: columnLeft + t.x + localX + (side === "r" ? 6 : -2),
    y: columnCenterY + t.y,
  };
}
