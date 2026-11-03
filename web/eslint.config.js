// ESLint（2026-11-03 起）。只收代码质量，不管格式；文案与引擎名的边界检查仍在
// `scripts/check-boundaries.mjs`。规则只开到"现有代码本来就满足"的那一档——
// 这份配置是护栏不是改造计划，哪条规则需要大面积改写就先不开。
import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/", "node_modules/", "public/"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    languageOptions: { globals: { ...globals.browser, ...globals.es2022 } },
    linterOptions: { reportUnusedDisableDirectives: "off" },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      /* 依赖数组里刻意漏掉的那些（ref / 稳定的 setter / "只在挂载时"）在仓库里
         是有意为之且有注释的；这条规则按现状会报几十处，所以先不作为错误。
         React Compiler 那一组（refs / set-state-in-effect / purity …）同理，
         它们要求的是另一种写法，不是现有代码的 bug。 */
      "react-hooks/exhaustive-deps": "off",
      "@typescript-eslint/no-unused-vars": ["error", {
        argsIgnorePattern: "^_",
        varsIgnorePattern: "^_",
        caughtErrorsIgnorePattern: "^_",
        destructuredArrayIgnorePattern: "^_",
      }],
      /* ANSI / 控制字符的清洗正则（`userFacingErrors.ts` / `artifactApi.ts`）本来就要匹配控制字符。 */
      "no-control-regex": "off",
      /* 重新抛出时要不要带 `cause` 是接口层的决定，不在这次护栏里。 */
      "preserve-caught-error": "off",
    },
  },
  {
    files: ["scripts/**/*.mjs", "*.config.{js,ts}"],
    languageOptions: { globals: { ...globals.node } },
  },
);
