<template>
  <div class="chat-bubble" :class="[message.role, { streaming: message.streaming }]">
    <div class="avatar">
      <component :is="message.role === 'user' ? UserOutlined : RobotOutlined" />
    </div>
    <div class="bubble-body">
      <div class="bubble-meta">
        <span class="role-label">{{ message.role === 'user' ? 'You' : 'Agent' }}</span>
        <span class="time-label" v-if="message.time">{{ message.time }}</span>
      </div>

      <!-- 工具调用过程 -->
      <div v-if="message.toolEvents && message.toolEvents.length" class="tool-events">
        <div v-for="(ev, i) in message.toolEvents" :key="i" class="tool-event">
          <CheckCircleOutlined v-if="ev.status === 'done'" class="done" />
          <LoadingOutlined v-else class="loading" />
          <span class="tool-name">{{ ev.display_name || ev.tool }}</span>
        </div>
      </div>

      <!-- 消息内容（支持简单 markdown） -->
      <div class="bubble-content" v-html="renderedContent"></div>

      <!-- 打字指示器 -->
      <div v-if="message.streaming && !message.content" class="typing-indicator">
        <span></span><span></span><span></span>
      </div>
    </div>
  </div>
</template>

<script>
import { computed, defineComponent } from 'vue'
import { UserOutlined, RobotOutlined, CheckCircleOutlined, LoadingOutlined } from '@ant-design/icons-vue'

export default defineComponent({
  name: 'ChatBubble',
  components: { UserOutlined, RobotOutlined, CheckCircleOutlined, LoadingOutlined },
  props: {
    message: {
      type: Object,
      required: true
    }
  },
  setup (props) {
    const renderedContent = computed(() => {
      const raw = props.message.content || ''
      // Step 1: HTML entity encode (escape everything)
      let safe = raw
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#x27;')

      // Step 2: Apply allowed markdown transforms (safe because input is already escaped)
      safe = safe
        .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre class="code-block"><code>$2</code></pre>')
        .replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\n/g, '<br>')

      // Step 3: Strip any remaining raw HTML tags that may have survived
      // (defense-in-depth — shouldn't be needed but belt-and-suspenders)
      safe = safe.replace(/<(?!\/?(strong|code|pre|br)\b)[^>]+>/gi, '')

      return safe
    })

    return { renderedContent }
  }
})
</script>

<style scoped lang="less">
.chat-bubble {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
  animation: fadeInUp 0.3s ease;

  &.assistant {
    .avatar {
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    }
    .bubble-body {
      max-width: 72%;
    }
    .bubble-content {
      background: #f0f2f5;
      color: #333;
    }
  }

  &.user {
    flex-direction: row-reverse;
    .avatar {
      background: linear-gradient(135deg, #1890ff 0%, #096dd9 100%);
    }
    .bubble-body {
      align-items: flex-end;
    }
    .bubble-content {
      background: linear-gradient(135deg, #1890ff 0%, #096dd9 100%);
      color: #fff;
      border-radius: 18px 18px 4px 18px;
    }
  }

  &.streaming .bubble-content::after {
    content: '▊';
    animation: blink 1s infinite;
    margin-left: 2px;
  }
}

.avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: #fff;
  font-size: 16px;
}

.bubble-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.bubble-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #999;
}

.bubble-content {
  padding: 12px 16px;
  border-radius: 18px 18px 18px 4px;
  font-size: 14px;
  line-height: 1.7;
  word-break: break-word;
  max-width: 100%;

  :deep(pre.code-block) {
    background: #1e1e1e;
    color: #d4d4d4;
    padding: 12px;
    border-radius: 8px;
    overflow-x: auto;
    font-size: 13px;
    margin: 8px 0;
  }

  :deep(code.inline-code) {
    background: rgba(0, 0, 0, 0.06);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 13px;
  }
}

.tool-events {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 4px;

  .tool-event {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    color: #666;
    background: #f5f5f5;
    padding: 2px 8px;
    border-radius: 12px;

    .done {
      color: #52c41a;
    }
    .loading {
      color: #1890ff;
    }
  }
}

.typing-indicator {
  display: flex;
  gap: 4px;
  padding: 8px 0;

  span {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #999;
    animation: bounce 1.4s infinite;
    &:nth-child(2) {
      animation-delay: 0.2s;
    }
    &:nth-child(3) {
      animation-delay: 0.4s;
    }
  }
}

@keyframes fadeInUp {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes bounce {
  0%,
  80%,
  100% {
    transform: scale(0);
  }
  40% {
    transform: scale(1);
  }
}

@keyframes blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0;
  }
}
</style>
