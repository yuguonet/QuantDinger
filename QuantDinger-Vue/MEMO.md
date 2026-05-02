# QuantDinger Frontend 备忘录

**技术栈：** Vue 2.6 + Ant Design Vue 1.x + Vuex + Vue Router + ECharts 6 + KLineCharts 9  
**模板基础：** vue-antd-pro 改造  
**入口：** `src/main.js`

---

## 目录结构

```
QuantDinger-Vue/
├── src/
│   ├── main.js                       # 应用入口
│   ├── App.vue                       # 根组件
│   ├── permission.js                 # 路由守卫（权限控制）
│   │
│   ├── config/                       # 配置
│   │   ├── router.config.js          # 路由定义（核心）
│   │   ├── defaultSettings.js        # 布局默认配置
│   │   └── aiModels.js               # AI 模型配置
│   │
│   ├── api/                          # 后端 API 调用层（16个模块）
│   │   ├── auth.js                   # 认证
│   │   ├── login.js                  # 登录
│   │   ├── user.js                   # 用户
│   │   ├── manage.js                 # 管理
│   │   ├── market.js                 # 行情
│   │   ├── global-market.js          # 全球市场
│   │   ├── dashboard.js              # 仪表盘
│   │   ├── strategy.js               # 策略
│   │   ├── agent.js                  # AI Agent
│   │   ├── ai-trading.js             # AI 交易
│   │   ├── fast-analysis.js          # 快速分析
│   │   ├── portfolio.js              # 投资组合
│   │   ├── quick-trade.js            # 快速交易
│   │   ├── polymarket.js             # Polymarket
│   │   ├── billing.js                # 计费
│   │   ├── credentials.js            # 凭据
│   │   └── settings.js               # 设置
│   │
│   ├── views/                        # 页面视图
│   │   ├── dashboard/                # 仪表盘首页
│   │   │   └── index.vue
│   │   │
│   │   ├── xuangu/                   # 🎯 选股器
│   │   │   ├── index.vue
│   │   │   ├── index.html
│   │   │   └── README.md
│   │   │
│   │   ├── shichang/                 # 📊 市场看板
│   │   │   ├── index.vue             # 主页面
│   │   │   ├── HotListCard.vue       # 热门榜单
│   │   │   ├── DragonTigerCard.vue   # 龙虎榜
│   │   │   ├── StrongStocksCard.vue  # 强势股
│   │   │   ├── StreakCard.vue        # 连板统计
│   │   │   ├── MacroCard.vue         # 宏观数据
│   │   │   ├── PeripheralMarketCard.vue # 外围市场
│   │   │   └── DetailModal.vue       # 详情弹窗
│   │   │
│   │   ├── ai-agent/                 # 🤖 AI 智能体
│   │   │   └── index.vue
│   │   │
│   │   ├── ai-analysis/              # AI 分析
│   │   │   └── index.vue
│   │   │
│   │   ├── ai-asset-analysis/        # 💰 AI 价值分析（默认首页）
│   │   │   └── index.vue
│   │   │
│   │   ├── indicator-ide/            # 🧪 指标 IDE
│   │   │   └── index.vue             # 图表+代码编辑+回测一体化
│   │   │
│   │   ├── indicator-community/      # 指标社区
│   │   │   └── index.vue
│   │   │
│   │   ├── trading-assistant/        # 📈 策略与实盘
│   │   │   └── index.vue             # 指标信号策略管理
│   │   │
│   │   ├── trading-bot/              # 🤖 交易机器人监控
│   │   │   └── index.vue
│   │   │
│   │   ├── portfolio/                # 投资组合
│   │   │   └── index.vue
│   │   │
│   │   ├── billing/                  # 计费
│   │   │   └── index.vue
│   │   │
│   │   ├── settings/                 # 设置
│   │   │   └── index.vue
│   │   │
│   │   ├── profile/                  # 个人资料
│   │   │   └── index.vue
│   │   │
│   │   ├── user/                     # 用户认证
│   │   │   ├── Login.vue
│   │   │   └── RegisterResult.vue
│   │   │
│   │   ├── user-manage/              # 用户管理
│   │   │   └── index.vue
│   │   │
│   │   ├── exception/                # 异常页面
│   │   │   ├── 403.vue
│   │   │   ├── 404.vue
│   │   │   └── 500.vue
│   │   │
│   │   └── 404.vue
│   │
│   ├── layouts/                      # 布局组件
│   │   ├── BasicLayout.vue           # 主布局（侧栏+顶栏+内容区）
│   │   ├── BasicLayout.less
│   │   ├── BlankLayout.vue           # 空白布局
│   │   ├── UserLayout.vue            # 登录页布局
│   │   ├── PageView.vue              # 页面容器
│   │   ├── RouteView.vue             # 路由视图
│   │   └── index.js
│   │
│   ├── components/                   # 公共组件
│   │   ├── Dialog.js                 # 对话框
│   │   ├── index.js
│   │   └── index.less
│   │
│   ├── store/                        # Vuex 状态管理
│   │   ├── index.js                  # Store 入口
│   │   ├── getters.js                # Getters
│   │   ├── mutation-types.js         # Mutation 类型常量
│   │   ├── app-mixin.js              # App mixin
│   │   ├── device-mixin.js           # 设备 mixin
│   │   └── i18n-mixin.js             # 国际化 mixin
│   │
│   ├── router/                       # 路由
│   │   ├── index.js                  # Router 实例
│   │   ├── generator-routers.js      # 动态路由生成器
│   │   └── README.md
│   │
│   ├── core/                         # 核心初始化
│   │   ├── bootstrap.js              # 启动引导
│   │   ├── icons.js                  # 图标注册
│   │   ├── use.js                    # 插件注册
│   │   └── lazy_use.js               # 懒加载插件
│   │
│   ├── utils/                        # 工具库
│   │   ├── request.js                # Axios 封装（请求拦截/响应处理）
│   │   ├── axios.js                  # Axios 实例
│   │   ├── exchangeWs.js             # WebSocket 交易所连接
│   │   ├── codeDecrypt.js            # 代码解密
│   │   ├── domUtil.js                # DOM 工具
│   │   ├── filter.js                 # 过滤器
│   │   ├── routeConvert.js           # 路由转换
│   │   ├── screenLog.js              # 屏幕日志
│   │   ├── userTime.js               # 用户时间
│   │   ├── util.js                   # 通用工具
│   │   └── utils.less
│   │
│   ├── locales/                      # 国际化
│   │   └── index.js
│   │
│   ├── mock/                         # Mock 数据
│   │   ├── index.js
│   │   └── util.js
│   │
│   ├── assets/                       # 静态资源
│   │   ├── background.svg
│   │   ├── logo.png / logo.svg / logo_w.png
│   │   └── slogo.png
│   │
│   ├── global.less                   # 全局样式
│   └── qd-layout-dark-override.less  # 暗色主题覆盖
│
├── public/                           # 公共静态文件
│   ├── index.html                    # HTML 模板
│   ├── avatar2.jpg
│   ├── logo.png / slogo.png
│   └── maps/                         # 地图数据
│       ├── world.json
│       └── world-atlas.json
│
├── config/                           # Webpack 配置
│   ├── plugin.config.js
│   └── themePluginConfig.js
│
├── deploy/                           # 部署配置
│   ├── nginx.conf
│   ├── nginx-docker.conf
│   └── caddy.conf
│
├── tests/                            # 测试
│   └── unit/
│       └── .eslintrc.js
│
├── vue.config.js                     # Vue CLI 配置
├── babel.config.js                   # Babel 配置
├── jest.config.js                    # Jest 测试配置
├── postcss.config.js                 # PostCSS
├── .eslintrc.js / .eslintrc.json     # ESLint
├── .stylelintrc.js                   # Stylelint
├── .prettierrc                       # Prettier
├── commitlint.config.js              # Commit 规范
├── .lintstagedrc.json                # Lint-staged
├── package.json                      # 依赖清单
├── Dockerfile                        # Docker 构建
├── build-docker.sh / build-docker.ps1
├── README.md / README_CN.md
└── MERGE_README.md
```

---

## 路由与页面对应

| 路由 | 组件 | 功能 |
|------|------|------|
| `/ai-asset-analysis` | `ai-asset-analysis/index.vue` | **默认首页**，AI 资产价值分析 |
| `/xuangu` | `xuangu/index.vue` | 选股器，条件筛选 |
| `/shichang` | `shichang/index.vue` | 市场看板（涨停池/龙虎榜/资金流/宏观） |
| `/ai-agent` | `ai-agent/index.vue` | AI 智能体对话 |
| `/ai-analysis` | `ai-analysis/index.vue` | AI 分析 |
| `/indicator-ide` | `indicator-ide/index.vue` | 指标 IDE（图表+代码+回测） |
| `/indicator-community` | `indicator-community/index.vue` | 指标社区分享 |
| `/strategy-live` | `trading-assistant/index.vue` | 策略与实盘（指标信号策略） |
| `/strategy-script` | `trading-assistant/index.vue` | Python 脚本策略（隐藏路由） |
| `/trading-bot` | `trading-bot/index.vue` | 交易机器人运维监控 |
| `/portfolio` | `portfolio/index.vue` | 投资组合管理 |
| `/billing` | `billing/index.vue` | 订阅/计费 |
| `/settings` | `settings/index.vue` | 系统设置 |
| `/profile` | `profile/index.vue` | 个人资料 |
| `/dashboard` | `dashboard/index.vue` | 仪表盘 |
| `/user/login` | `user/Login.vue` | 登录 |
| `/user/register` | `user/RegisterResult.vue` | 注册结果 |
| `/user-manage` | `user-manage/index.vue` | 用户管理 |

---

## 关键依赖

| 库 | 版本 | 用途 |
|----|------|------|
| vue | ^2.6.14 | 核心框架 |
| vuex | ^3.6.2 | 状态管理 |
| vue-router | ^3.5.3 | 路由 |
| ant-design-vue | ^1.7.8 | UI 组件库 |
| @ant-design-vue/pro-layout | ^1.0.12 | Pro 布局 |
| echarts | ^6.0.0 | 图表 |
| klinecharts | ^9.8.0 | K线图表 |
| lightweight-charts | ^5.0.8 | 轻量图表 |
| element-ui | ^2.15.14 | 补充 UI 组件 |
| axios | ^0.26.1 | HTTP 请求 |
| codemirror | ^5.65.16 | 代码编辑器 |
| moment | ^2.29.2 | 时间处理 |
| crypto-js | ^4.2.0 | 加密 |
| vue-i18n | ^8.27.1 | 国际化 |
| viser-vue | ^2.4.8 | Viser 图表 |
| @antv/data-set | ^0.10.2 | 数据处理 |
| wangeditor | ^3.1.1 | 富文本编辑器 |
| vue-quill-editor | ^3.0.6 | Quill 编辑器 |
| vue-cropper | 0.4.9 | 图片裁剪 |
| vue-clipboard2 | ^0.2.1 | 剪贴板 |

---

## 架构特点

- **Ant Design Pro 模板**：成熟的企业级中后台脚手架
- **双图表库**：ECharts（通用图表）+ KLineCharts（专业K线）
- **双编辑器**：CodeMirror（代码）+ WangEditor/Quill（富文本）
- **动态路由**：后端控制菜单权限，前端 `generator-routers.js` 动态生成
- **WebSocket**：`exchangeWs.js` 实现交易所实时数据推送
- **暗色主题**：`qd-layout-dark-override.less` 提供暗色模式
- **国际化**：vue-i18n 支持多语言
- **代码编辑**：指标 IDE 集成 CodeMirror，支持在线编写策略代码
