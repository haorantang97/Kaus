import { describe, expect, it } from "vitest";
import { wheelAnchor, wheelNodeTransform, wheelTransformCss } from "./wheelGeometry";

/* 对拍用的**独立参考实现**：把 CSS transform 真的当成 4×4 矩阵乘出来，
 * 再做 perspective(900) 的透视除法，最后按 transform-origin: left center 平移回去。
 * 解析式（wheelAnchor）如果和它差 > 1px，就说明公式和 CSS 已经漂移了。
 *
 * 之所以不用 jsdom 量 DOM：jsdom 不做布局，getBoundingClientRect 恒为 0，
 * 量不出任何东西。真实浏览器里的对拍走 Playwright（docs/frontend/screenshots）。
 */

type Vec4 = [number, number, number, number];
type Mat4 = number[]; // row-major 4×4

const I: Mat4 = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];

function mul(a: Mat4, b: Mat4): Mat4 {
  const out = new Array(16).fill(0) as Mat4;
  for (let r = 0; r < 4; r++) {
    for (let c = 0; c < 4; c++) {
      let sum = 0;
      for (let k = 0; k < 4; k++) sum += a[r * 4 + k] * b[k * 4 + c];
      out[r * 4 + c] = sum;
    }
  }
  return out;
}

function apply(m: Mat4, v: Vec4): Vec4 {
  const out = [0, 0, 0, 0] as Vec4;
  for (let r = 0; r < 4; r++) {
    out[r] = m[r * 4] * v[0] + m[r * 4 + 1] * v[1] + m[r * 4 + 2] * v[2] + m[r * 4 + 3] * v[3];
  }
  return out;
}

const translate = (x: number, y: number): Mat4 => [1, 0, 0, x, 0, 1, 0, y, 0, 0, 1, 0, 0, 0, 0, 1];
const scaleM = (s: number): Mat4 => [s, 0, 0, 0, 0, s, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
const rotateX = (deg: number): Mat4 => {
  const r = (deg * Math.PI) / 180;
  const c = Math.cos(r);
  const s = Math.sin(r);
  return [1, 0, 0, 0, 0, c, -s, 0, 0, s, c, 0, 0, 0, 0, 1];
};
const perspective = (p: number): Mat4 => [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, -1 / p, 1];

/** 参考实现：节点左/右中点在 `.ag-cols` 坐标系里的位置。 */
function referenceAnchor(opts: {
  columnLeft: number;
  columnCenterY: number;
  width: number;
  height: number;
  offset: number;
  side: "l" | "r";
}): { x: number; y: number } {
  const t = wheelNodeTransform(opts.offset);
  // transform-origin: left center → 局部原点在布局盒的左中点。
  // 布局盒：left: 0（列内）、top: 50% → 盒顶在列中心线上，故左中点在 columnCenterY + height/2。
  const originX = opts.columnLeft;
  const originY = opts.columnCenterY + opts.height / 2;
  // translateY(calc(-50% + y)) translateX(x) rotateX(r) scale(s)，从左往右依次左乘。
  let m: Mat4 = I;
  m = mul(m, translate(0, -opts.height / 2 + t.y));
  m = mul(m, translate(t.x, 0));
  m = mul(m, rotateX(t.rotate));
  m = mul(m, scaleM(t.scale));
  const local: Vec4 = [opts.side === "r" ? opts.width : 0, 0, 0, 1];
  const moved = apply(m, local);
  // perspective 挂在 .ag-wheel-col 上，作用于其子元素相对于列中心的位置。
  const projected = apply(perspective(900), [moved[0], moved[1], moved[2], moved[3]]);
  const w = projected[3] === 0 ? 1 : projected[3];
  return {
    x: originX + projected[0] / w + (opts.side === "r" ? 6 : -2),
    y: originY + projected[1] / w,
  };
}

describe("轮盘几何", () => {
  it("解析端点与矩阵参考实现误差 ≤ 1px（各种 offset / 两侧）", () => {
    const columnLeft = 142.5;
    const columnCenterY = 255.25;
    const width = 336.75;
    const height = 42;
    for (const offset of [-6, -4.5, -3, -2, -1.25, -1, -0.5, 0, 0.4, 1, 2, 3.75, 5, 7]) {
      for (const side of ["l", "r"] as const) {
        const got = wheelAnchor({ columnLeft, columnCenterY, width, offset, side });
        const want = referenceAnchor({ columnLeft, columnCenterY, width, height, offset, side });
        expect(Math.abs(got.x - want.x), `x @ offset=${offset} side=${side}`).toBeLessThanOrEqual(1);
        expect(Math.abs(got.y - want.y), `y @ offset=${offset} side=${side}`).toBeLessThanOrEqual(1);
      }
    }
  });

  it("端点与节点高度无关（translateY(-50%) 抵消掉布局盒高度）", () => {
    const base = { columnLeft: 0, columnCenterY: 100, width: 300, offset: 1.5, side: "r" as const };
    const a = referenceAnchor({ ...base, height: 30 });
    const b = referenceAnchor({ ...base, height: 90 });
    expect(Math.abs(a.y - b.y)).toBeLessThan(1e-9);
    expect(Math.abs(wheelAnchor(base).y - a.y)).toBeLessThan(1e-9);
  });

  it("前排节点（offset=0）没有旋转与位移，端点贴住节点边缘", () => {
    const t = wheelNodeTransform(0);
    expect(t.rotate).toBe(-0);
    expect(t.x).toBe(-0);
    expect(t.y).toBe(0);
    expect(t.scale).toBeCloseTo(1.18, 6);
    const right = wheelAnchor({ columnLeft: 10, columnCenterY: 200, width: 330, offset: 0, side: "r" });
    expect(right.x).toBeCloseTo(10 + 1.18 * 330 + 6, 6);
    expect(right.y).toBeCloseTo(200, 6);
  });

  it("transform 字符串与公式同源", () => {
    expect(wheelTransformCss(wheelNodeTransform(2))).toBe(
      "translateY(calc(-50% + 116.0px)) translateX(-18.0px) rotateX(-14.00deg) scale(0.800)",
    );
  });
});
