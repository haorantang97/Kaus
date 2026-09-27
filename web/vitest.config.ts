import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 测试与 Vite 同源（component-boundaries §7 推荐的 vitest + testing-library），
// 配置刻意与 vite.config.ts 分开：跑测试不需要 tailwind 插件与 dev proxy。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./src/test/setup.ts"],
  },
});
