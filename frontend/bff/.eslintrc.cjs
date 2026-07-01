/* eslint config — typed lint for the BFF (the security boundary; keep it strict). */
module.exports = {
  root: true,
  env: { node: true, es2022: true },
  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 2022,
    sourceType: 'module',
  },
  plugins: ['@typescript-eslint'],
  extends: ['eslint:recommended', 'plugin:@typescript-eslint/recommended'],
  rules: {
    '@typescript-eslint/no-explicit-any': 'warn',
    '@typescript-eslint/no-unused-vars': [
      'error',
      { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
    ],
    'no-console': 'error',
    eqeqeq: ['error', 'always'],
  },
  ignorePatterns: ['dist/', 'node_modules/', 'coverage/', '*.cjs'],
  overrides: [
    {
      files: ['test/**/*.ts'],
      env: { node: true },
      rules: {
        '@typescript-eslint/no-explicit-any': 'off',
      },
    },
  ],
};
