import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

// Flat config for the Vite + React + TypeScript stack (replaced
// eslint-config-next when the app moved off Next.js). The rule set stays
// deliberately relaxed, matching the previous config.
export default tseslint.config(
  {
    ignores: [
      "node_modules/**",
      "dist/**",
      "dev.log",
      "mini-services/**",
      "examples/**",
      "upload/**",
      "download/**",
      "tool-results/**",
      "db/**",
      "skills",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      globals: { ...globals.browser },
    },
    plugins: {
      "react-hooks": reactHooks,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Previous eslint-config-next setup had these disabled; keep the
      // codebase lint-clean without churning source files.
      "react-hooks/exhaustive-deps": "off",
      "react-hooks/purity": "off",
      "react-hooks/set-state-in-effect": "off", // new rule in react-hooks v7; stock shadcn code trips it
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-unused-vars": "off",
      "@typescript-eslint/no-non-null-assertion": "off",
      "@typescript-eslint/ban-ts-comment": "off",
      "@typescript-eslint/prefer-as-const": "off",
      "@typescript-eslint/no-unused-expressions": "off",
      "no-unused-vars": "off",
      "no-undef": "off", // TypeScript handles this; avoids false positives on ambient types
      "prefer-const": "off",
      "no-console": "off",
      "no-debugger": "off",
      "no-empty": "off",
      "no-irregular-whitespace": "off",
      "no-case-declarations": "off",
      "no-fallthrough": "off",
      "no-mixed-spaces-and-tabs": "off",
      "no-redeclare": "off",
      "no-unreachable": "off",
      "no-useless-escape": "off",
    },
  },
);
