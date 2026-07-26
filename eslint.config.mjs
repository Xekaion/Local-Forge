import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    files: ["app/page.tsx"],
    rules: {
      "react-hooks/set-state-in-effect": "off",
    },
  },
  globalIgnores([
    ".next/**",
    ".vinext/**",
    "out/**",
    "build/**",
    "dist/**",
    "work/**",
    "engine/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
