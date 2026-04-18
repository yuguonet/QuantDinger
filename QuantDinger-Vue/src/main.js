// with polyfills
import 'core-js/stable'
import 'regenerator-runtime/runtime'

import Vue from 'vue'
import App from './App.vue'
import router from './router'
import store from './store/'
import i18n from './locales'
import { VueAxios } from './utils/request'
import ProLayout, { PageHeaderWrapper } from '@ant-design-vue/pro-layout'
import themePluginConfig from '../config/themePluginConfig'

// mock
// WARNING: `mockjs` NOT SUPPORT `IE` PLEASE DO NOT USE IN `production` ENV.
import './mock'

import bootstrap from './core/bootstrap'
import './core/lazy_use' // use lazy load components
import './permission' // permission control
import './utils/filter' // global filter
import './global.less' // global style
// 必须在 global.less 之后：覆盖 Pro Layout 顶栏/侧栏底栏（否则会被 global 与组件顺序盖住）
import './qd-layout-dark-override.less'

Vue.config.productionTip = false

// Suppress noisy ResizeObserver loop errors (harmless in most cases on responsive layouts)
if (typeof window !== 'undefined') {
  const ignoreResizeObserverError = (e) => {
    const msg = (e && (e.reason && e.reason.message || e.message)) || ''
    if (msg.includes('ResizeObserver loop') || msg.includes('ResizeObserver loop limit exceeded')) {
      e.preventDefault && e.preventDefault()
      e.stopImmediatePropagation && e.stopImmediatePropagation()
      return false
    }
  }
  window.addEventListener('error', ignoreResizeObserverError)
  window.addEventListener('unhandledrejection', ignoreResizeObserverError)
}

// mount axios to `Vue.$http` and `this.$http`
Vue.use(VueAxios)
// use pro-layout components
Vue.component('ProLayout', ProLayout)
Vue.component('PageContainer', PageHeaderWrapper)
Vue.component('PageHeaderWrapper', PageHeaderWrapper)

window.umi_plugin_ant_themeVar = themePluginConfig.theme

new Vue({
  router,
  store,
  i18n,
  // init localstorage, vuex, Logo message
  created: bootstrap,
  render: h => h(App)
}).$mount('#app')
