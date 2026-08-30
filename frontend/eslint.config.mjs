import js from '@eslint/js'
import globals from 'globals'
import tseslint from 'typescript-eslint'

/**
 * eslint-config-next намеренно не подключается: он рассчитан на старый формат
 * конфигурации и в плоском падает с «Failed to patch ESLint». Правила самого
 * Next заменяет строгая проверка типов — она ловит больше и в том же прогоне.
 */
export default tseslint.config(
  {
    ignores: [
      '.next/**',
      'out/**',
      'node_modules/**',
      'src/api/schema.ts',
      'next-env.d.ts',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/consistent-type-imports': 'error',
    },
  },
)
