const path = require('path')
// Try Vue CLI's bundled webpack first, fall back to root-level webpack
let webpack
try {
  webpack = require('@vue/cli-service/node_modules/webpack')
} catch (e) {
  webpack = require('webpack')
}
const packageJson = require('./package.json')
const fs = require('fs')
let GitRevision = { version: () => 'unknown' }
// Only init git plugin if .git directory exists (avoid "not a git repo" crash)
if (fs.existsSync(path.join(__dirname, '.git'))) {
  try {
    const GitRevisionPlugin = require('git-revision-webpack-plugin')
    GitRevision = new GitRevisionPlugin()
  } catch (e) {}
}
const buildDate = JSON.stringify(new Date().toLocaleString())
const createThemeColorReplacerPlugin = require('./config/plugin.config')

function resolve (dir) {
  return path.join(__dirname, dir)
}

// check Git
function getGitHash () {
  try {
    return GitRevision.version()
  } catch (e) {
    return 'unknown'
  }
}
// eslint-disable-next-line no-unused-vars
const isProd = process.env.NODE_ENV === 'production'
// eslint-disable-next-line no-unused-vars
const assetsCDN = {
  // webpack build externals
  externals: {
    vue: 'Vue',
    'vue-router': 'VueRouter',
    vuex: 'Vuex',
    axios: 'axios'
  },
  css: [],
  // https://unpkg.com/browse/vue@2.6.10/
  js: [
    '//cdn.jsdelivr.net/npm/vue@2.6.14/dist/vue.min.js',
    '//cdn.jsdelivr.net/npm/vue-router@3.5.1/dist/vue-router.min.js',
    '//cdn.jsdelivr.net/npm/vuex@3.1.1/dist/vuex.min.js',
    '//cdn.jsdelivr.net/npm/axios@0.21.1/dist/axios.min.js'
  ]
}

// vue.config.js
const vueConfig = {
  configureWebpack: {
    // webpack plugins
    plugins: [
      // Ignore all locale files of moment.js
      new webpack.IgnorePlugin({
        contextRegExp: /^\.\/locale$/,
        resourceRegExp: /moment$/
      }),
      new webpack.DefinePlugin({
        APP_VERSION: `"${packageJson.version}"`,
        GIT_HASH: JSON.stringify(getGitHash()),
        BUILD_DATE: buildDate
      })
    ]
    // en_US: `if prod, add externals`
    // zh_CN: `这里是用来控制编译忽略外部依赖的，与 config.plugin('html') 配合可以编译时引入外部CDN文件依赖`
    // externals: isProd ? assetsCDN.externals : {}
  },

  chainWebpack: config => {
    config.resolve.alias.set('@$', resolve('src'))

    // Fix: Vue CLI 5 + webpack 4 root dep causes ProgressPlugin options validation error
    // Remove the built-in progress plugin to avoid schema conflict between webpack 4/5
    config.plugins.delete('progress')

    // fixed svg-loader by https://github.com/damianstasik/vue-svg-loader/issues/185#issuecomment-1126721069
		const svgRule = config.module.rule('svg')
		// Remove regular svg config from root rules list
		config.module.rules.delete('svg')

		config.module.rule('svg')
			// Use svg component rule
			.oneOf('svg_as_component')
				.resourceQuery(/inline/)
				.test(/\.(svg)(\?.*)?$/)
				.use('babel-loader')
					.loader('babel-loader')
					.end()
				.use('vue-svg-loader')
					.loader('vue-svg-loader')
					.options({
						svgo: {
							plugins: [
								{ prefixIds: true },
								{ cleanupIDs: true },
								{ convertShapeToPath: false },
								{ convertStyleToAttrs: true }
							]
						}
					})
					.end()
				.end()
			// Otherwise use original svg rule
			.oneOf('svg_as_regular')
				.merge(svgRule.toConfig())
				.end()

    // en_US: If prod is on assets require on cdn
    // zh_CN: 如果是 prod 模式，则引入 CDN 依赖文件，有需要减少包大小请自行解除依赖
    //
    // if (isProd) {
    //   config.plugin('html').tap(args => {
    //     args[0].cdn = assetsCDN
    //     return args
    //   })
    // }
  },

  css: {
    loaderOptions: {
      less: {
        modifyVars: {
          // less vars，customize ant design theme

          // 'primary-color': '#F5222D',
          // 'link-color': '#F5222D',
          'border-radius-base': '2px'
        },
        // DO NOT REMOVE THIS LINE
        javascriptEnabled: true
      }
    }
  },

  devServer: {
    // development server port 8000
    port: 8000,
    proxy: {
      '/api': {
        target: 'http://localhost:5000',
        ws: true,
        changeOrigin: true,
        timeout: 600000, // 10 minutes for long-running requests like backtest
        proxyTimeout: 600000, // 10 minutes proxy timeout

        // ── SSE 流式穿透 ──
        // http-proxy 默认会缓冲整个 HTTP 响应再转发给客户端。
        // 对于 SSE（text/event-stream），这意味着前端在后端完成前
        // 收不到任何数据 → 进度条永远为 0%。
        // selfHandleResponse=true 后手动 pipe 响应流，跳过缓冲。
        selfHandleResponse: true,
        onProxyRes (proxyRes, req, res) {
          // 非 SSE 响应：让 http-proxy 默认处理（已接管，需手动 pipe）
          const ct = proxyRes.headers['content-type'] || ''
          if (!ct.includes('text/event-stream')) {
            // 写响应头
            res.writeHead(proxyRes.statusCode, proxyRes.headers)
            proxyRes.pipe(res)
            return
          }
          // SSE 响应：逐 chunk flush，确保前端实时收到
          const headers = Object.assign({}, proxyRes.headers)
          delete headers['content-length']       // 流式响应无固定长度
          delete headers['content-encoding']     // 禁止 gzip 压缩流
          headers['cache-control'] = 'no-cache'
          headers['x-accel-buffering'] = 'no'
          res.writeHead(proxyRes.statusCode, headers)
          proxyRes.on('data', (chunk) => {
            res.write(chunk)
            if (typeof res.flush === 'function') res.flush()
          })
          proxyRes.on('end', () => res.end())
          proxyRes.on('error', (err) => {
            console.error('[proxy] SSE stream error:', err.message)
            res.end()
          })
        }
      },
      // 腾讯行情接口（大盘指数，10秒刷新）
      '/qt': {
        target: 'https://qt.gtimg.cn',
        changeOrigin: true,
        pathRewrite: { '^/qt': '' }
      },
      // 东方财富 - 涨停池
      '/em-zt': {
        target: 'https://push2ex.eastmoney.com',
        changeOrigin: true,
        pathRewrite: { '^/em-zt': '/getTopicZTPool' }
      },
      // 东方财富 - 跌停池
      '/em-dt': {
        target: 'https://push2ex.eastmoney.com',
        changeOrigin: true,
        pathRewrite: { '^/em-dt': '/getTopicDTPool' }
      },
      // 东方财富 - 北向资金
      '/em-north': {
        target: 'https://push2.eastmoney.com',
        changeOrigin: true,
        pathRewrite: { '^/em-north': '/api/qt/kamt.rtmin/get' }
      },
      // 东方财富 - 涨跌家数
      '/em-overview': {
        target: 'https://push2.eastmoney.com',
        changeOrigin: true,
        pathRewrite: { '^/em-overview': '/api/qt/ulist.np/get' }
      },
      // 东方财富 - 连板池（预留）
      '/em-lb': {
        target: 'https://push2ex.eastmoney.com',
        changeOrigin: true,
        pathRewrite: { '^/em-lb': '/getTopicLBPool' }
      }
    }
  },

  // disable source map in production
  productionSourceMap: false,
  lintOnSave: undefined,
  // babel-loader no-ignore node_modules/*
  transpileDependencies: []
}

// Add ThemeColorReplacer plugin for theme color switching
// This plugin is needed in production to support dynamic theme color changes
vueConfig.configureWebpack.plugins.push(createThemeColorReplacerPlugin())

module.exports = vueConfig
