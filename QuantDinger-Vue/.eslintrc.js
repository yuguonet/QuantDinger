module.exports = {
  root: true,
  env: {
    node: true
  },
  'extends': [
    'plugin:vue/strongly-recommended',
    '@vue/standard'
  ],
  rules: {
    'no-console': 'off',
    'no-debugger': process.env.NODE_ENV === 'production' ? 'error' : 'off',
    'generator-star-spacing': 'off',
    'no-mixed-operators': 0,

    // --- 放宽格式规则 ---

    // 允许多个属性在同一行（单行最多 10 个，多行每个元素最多 3 个）
    'vue/max-attributes-per-line': [2, {
      'singleline': 10,
      'multiline': {
        'max': 3,
        'allowFirstLine': true
      }
    }],

    'vue/attribute-hyphenation': 0,
    'vue/html-self-closing': 0,
    'vue/component-name-in-template-casing': 0,
    'vue/html-closing-bracket-spacing': 0,
    'vue/singleline-html-element-content-newline': 0,
    'vue/no-unused-components': 0,
    'vue/multiline-html-element-content-newline': 0,
    'vue/no-use-v-if-with-v-for': 0,
    'vue/html-closing-bracket-newline': 0,
    'vue/no-parsing-error': 0,

    // Vue 2 项目不需要此规则
    'vue/no-v-model-argument': 0,

    'no-tabs': 0,
    'quotes': [
      2,
      'single',
      {
        'avoidEscape': true,
        'allowTemplateLiterals': true
      }
    ],
    'semi': [
      2,
      'never',
      {
        'beforeStatementContinuationChars': 'never'
      }
    ],
    'no-delete-var': 2,
    'prefer-const': [2, {
      'ignoreReadBeforeAssign': false
    }],
    'template-curly-spacing': 'off',
    'indent': 'off',

    // --- 放宽以下规则为 warn 或 off ---

    // 允许尾逗号（不强制有无）
    'comma-dangle': 'off',

    // 函数括号前空格不强制
    'space-before-function-paren': 'off',

    // 允许 let a, b 多变量声明
    'one-var': 'off',

    // 花括号风格不强制（允许 } else { 和 } \n else {）
    'brace-style': 'off',

    // 允许多余空格（对齐注释等）
    'no-multi-spaces': 'off',

    // 允许对象属性在同一行
    'object-property-newline': 'off',

    // 允许行尾空格
    'no-trailing-spaces': 'off'
  },
  parserOptions: {
    parser: 'babel-eslint'
  },
  overrides: [
    {
      files: [
        '**/__tests__/*.{j,t}s?(x)',
        '**/tests/unit/**/*.spec.{j,t}s?(x)'
      ],
      env: {
        jest: true
      }
    }
  ]
}
