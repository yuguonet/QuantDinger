<template>
  <div class="cron-jobs-page">
    <!-- 顶部状态栏 -->
    <div class="status-bar">
      <div class="status-left">
        <a-icon type="clock-circle" class="header-icon" />
        <span class="header-title">定时任务</span>
        <a-tag :color="workerAlive ? 'green' : 'red'" class="status-tag">
          {{ workerAlive ? '运行中 (' + scheduledCount + ' 个调度)' : '未启动' }}
        </a-tag>
        <span class="job-count">
          共 {{ jobs.length }} 个任务，{{ enabledCount }} 个启用
        </span>
      </div>
      <div class="status-right">
        <a-button type="primary" @click="showCreateModal">
          <a-icon type="plus" /> 新建任务
        </a-button>
        <a-button style="margin-left: 8px" @click="fetchJobs">
          <a-icon type="reload" /> 刷新
        </a-button>
      </div>
    </div>

    <!-- 实时事件通知 -->
    <transition-group name="event-slide" tag="div" class="event-feed">
      <div
        v-for="ev in recentEvents"
        :key="ev.id"
        :class="['event-item', 'event-' + ev.type]"
      >
        <a-icon :type="eventIcon(ev.type)" />
        <span class="event-time">{{ ev.timestamp }}</span>
        <span class="event-name">{{ ev.job_name }}</span>
        <span class="event-msg">{{ eventMessage(ev) }}</span>
      </div>
    </transition-group>

    <!-- 任务列表 -->
    <a-table
      :columns="columns"
      :data-source="jobs"
      :loading="loading"
      row-key="id"
      :pagination="false"
      size="middle"
      style="margin-top: 16px"
    >
      <template #enabled="text, record">
        <a-switch
          :checked="record.enabled"
          size="small"
          @change="(checked) => toggleEnabled(record, checked)"
        />
      </template>

      <template #mode="text">
        <a-tag :color="text === 'prompt' ? 'blue' : 'green'">
          {{ text === 'prompt' ? 'Prompt' : 'Function' }}
        </a-tag>
      </template>

      <template #cron_expr="text">
        <a-tooltip :title="describeCron(text)">
          <code>{{ text }}</code>
        </a-tooltip>
      </template>

      <template #last_run_at="text">
        <span v-if="text">{{ text }}</span>
        <span v-else class="text-muted">从未执行</span>
      </template>

      <template #status="_, record">
        <a-tag v-if="record.last_error" color="red" :title="record.last_error">
          失败 ({{ record.error_count }})
        </a-tag>
        <a-tag v-else-if="record.last_success_at" color="green">
          正常
        </a-tag>
        <a-tag v-else color="default">
          待执行
        </a-tag>
      </template>

      <template #action="_, record">
        <a-space>
          <a-button size="small" @click="handleTrigger(record)" :loading="record._triggering">
            <a-icon type="play-circle" /> 触发
          </a-button>
          <a-button size="small" @click="showEditModal(record)">
            <a-icon type="edit" />
          </a-button>
          <a-popconfirm
            title="确定删除此任务？"
            @confirm="handleDelete(record)"
          >
            <a-button size="small" type="danger">
              <a-icon type="delete" />
            </a-button>
          </a-popconfirm>
        </a-space>
      </template>
    </a-table>

    <!-- 创建/编辑弹窗 -->
    <a-modal
      v-model="modalVisible"
      :title="editingJob ? '编辑定时任务' : '新建定时任务'"
      @ok="handleSubmit"
      @cancel="closeModal"
      :confirm-loading="submitting"
      width="600px"
    >
      <a-form-model
        ref="formRef"
        :model="form"
        :rules="rules"
        layout="vertical"
      >
        <a-form-model-item label="任务名称" prop="name">
          <a-input v-model="form.name" placeholder="如：盘后回溯验证" />
        </a-form-model-item>

        <a-form-model-item label="Cron 表达式" prop="cron_expr">
          <a-input v-model="form.cron_expr" placeholder="0 18 * * 1-5">
            <template #suffix>
              <a-tooltip :title="describeCron(form.cron_expr)">
                <a-icon type="question-circle" />
              </a-tooltip>
            </template>
          </a-input>
          <div class="cron-hint">
            格式：分 时 日 月 周。常用示例：
            <a @click="form.cron_expr = '0 18 * * 1-5'">工作日18:00</a>
            <a @click="form.cron_expr = '30 9 * * 1-5'">工作日9:30</a>
            <a @click="form.cron_expr = '*/5 9-15 * * 1-5'">盘中每5分钟</a>
            <a @click="form.cron_expr = '0 */2 * * *'">每2小时</a>
          </div>
        </a-form-model-item>

        <a-form-model-item label="执行模式" prop="mode">
          <a-radio-group v-model="form.mode">
            <a-radio-button value="prompt">
              <a-icon type="message" /> Prompt（调 Agent）
            </a-radio-button>
            <a-radio-button value="function">
              <a-icon type="code" /> Function（0 token）
            </a-radio-button>
          </a-radio-group>
        </a-form-model-item>

        <a-form-model-item
          v-if="form.mode === 'prompt'"
          label="Agent 消息"
          prop="prompt"
        >
          <a-textarea
            v-model="form.prompt"
            :rows="4"
            placeholder="如：检查今日板块涨跌排名，找出异动板块，简要汇报"
          />
        </a-form-model-item>

        <a-form-model-item
          v-if="form.mode === 'function'"
          label="函数路径"
          prop="function_path"
        >
          <a-input
            v-model="form.function_path"
            placeholder="app.agent.chain.evaluator.auto_evaluate"
          />
          <div class="cron-hint">
            常用：
            <a @click="form.function_path = 'app.agent.chain.evaluator.auto_evaluate'">盘后回溯验证</a>
          </div>
        </a-form-model-item>

        <a-form-model-item label="描述">
          <a-input v-model="form.description" placeholder="可选" />
        </a-form-model-item>
      </a-form-model>
    </a-modal>
  </div>
</template>

<script>
import {
  getCronJobs,
  createCronJob,
  updateCronJob,
  deleteCronJob,
  triggerCronJob,
  getCronStatus,
  createCronEventStream
} from '@/api/cron'

export default {
  name: 'CronJobs',
  data () {
    return {
      jobs: [],
      loading: false,
      workerAlive: false,
      scheduledCount: 0,
      modalVisible: false,
      submitting: false,
      editingJob: null,
      recentEvents: [],
      sseConnection: null,
      form: {
        name: '',
        cron_expr: '',
        mode: 'prompt',
        prompt: '',
        function_path: '',
        description: ''
      },
      rules: {
        name: [{ required: true, message: '请输入任务名称', trigger: 'blur' }],
        cron_expr: [
          { required: true, message: '请输入 cron 表达式', trigger: 'blur' },
          { validator: this.validateCron, trigger: 'blur' }
        ],
        prompt: [{ required: true, message: '请输入 Agent 消息', trigger: 'blur' }],
        function_path: [{ required: true, message: '请输入函数路径', trigger: 'blur' }]
      },
      columns: [
        { title: 'ID', dataIndex: 'id', width: 60 },
        { title: '名称', dataIndex: 'name', width: 160 },
        { title: '启用', dataIndex: 'enabled', scopedSlots: { customRender: 'enabled' }, width: 70 },
        { title: '模式', dataIndex: 'mode', scopedSlots: { customRender: 'mode' }, width: 90 },
        { title: 'Cron', dataIndex: 'cron_expr', scopedSlots: { customRender: 'cron_expr' }, width: 160 },
        { title: '上次执行', dataIndex: 'last_run_at', scopedSlots: { customRender: 'last_run_at' }, width: 170 },
        { title: '状态', scopedSlots: { customRender: 'status' }, width: 90 },
        { title: '累计', dataIndex: 'total_runs', width: 60 },
        { title: '操作', scopedSlots: { customRender: 'action' }, width: 200 }
      ]
    }
  },
  computed: {
    enabledCount () {
      return this.jobs.filter(j => j.enabled).length
    }
  },
  mounted () {
    this.fetchJobs()
    this.fetchStatus()
    this.connectSSE()
  },
  beforeDestroy () {
    if (this.sseConnection) {
      this.sseConnection.close()
    }
  },
  methods: {
    async fetchJobs () {
      this.loading = true
      try {
        const res = await getCronJobs()
        this.jobs = (res.jobs || []).map(j => ({ ...j, _triggering: false }))
      } catch (e) {
        this.$message.error('获取任务列表失败: ' + (e.message || e))
      } finally {
        this.loading = false
      }
    },

    async fetchStatus () {
      try {
        const res = await getCronStatus()
        this.workerAlive = (res.scheduled_count || 0) > 0
        this.scheduledCount = res.scheduled_count || 0
      } catch (e) {
        this.workerAlive = false
        this.scheduledCount = 0
      }
    },

    connectSSE () {
      this.sseConnection = createCronEventStream({
        onConnected: () => {
          this.workerAlive = true
        },
        onStart: (data) => {
          this.addEvent(data)
        },
        onSuccess: (data) => {
          this.addEvent(data)
          this.fetchJobs() // 刷新列表状态
        },
        onError: (data) => {
          this.addEvent(data)
          this.fetchJobs()
          this.$notification.warning({
            message: '定时任务失败',
            description: `${data.job_name}: ${data.error}`,
            duration: 8
          })
        }
      })
    },

    addEvent (data) {
      const ev = { ...data, id: Date.now() + Math.random() }
      this.recentEvents.unshift(ev)
      if (this.recentEvents.length > 20) {
        this.recentEvents = this.recentEvents.slice(0, 20)
      }
    },

    eventIcon (type) {
      return { job_start: 'loading', job_success: 'check-circle', job_error: 'close-circle' }[type] || 'info-circle'
    },

    eventMessage (ev) {
      if (ev.type === 'job_start') return '开始执行...'
      if (ev.type === 'job_success') return ev.result_preview || '执行成功'
      if (ev.type === 'job_error') return ev.error || '执行失败'
      return ''
    },

    showCreateModal () {
      this.editingJob = null
      this.form = {
        name: '',
        cron_expr: '0 18 * * 1-5',
        mode: 'prompt',
        prompt: '',
        function_path: '',
        description: ''
      }
      this.modalVisible = true
    },

    showEditModal (job) {
      this.editingJob = job
      this.form = {
        name: job.name,
        cron_expr: job.cron_expr,
        mode: job.mode || 'prompt',
        prompt: job.prompt || '',
        function_path: job.function_path || '',
        description: job.description || ''
      }
      this.modalVisible = true
    },

    closeModal () {
      this.modalVisible = false
      this.editingJob = null
    },

    async handleSubmit () {
      try {
        await this.$refs.formRef.validate()
      } catch (e) {
        return
      }

      this.submitting = true
      try {
        if (this.editingJob) {
          await updateCronJob(this.editingJob.id, this.form)
          this.$message.success('任务已更新')
        } else {
          await createCronJob(this.form)
          this.$message.success('任务已创建')
        }
        this.modalVisible = false
        this.fetchJobs()
      } catch (e) {
        this.$message.error((e.response?.data?.error || e.message || '操作失败'))
      } finally {
        this.submitting = false
      }
    },

    async toggleEnabled (job, checked) {
      try {
        await updateCronJob(job.id, { enabled: checked })
        job.enabled = checked
        this.$message.success(checked ? '已启用' : '已暂停')
      } catch (e) {
        this.$message.error('操作失败')
      }
    },

    async handleTrigger (job) {
      this.$set(job, '_triggering', true)
      try {
        await triggerCronJob(job.id)
        this.$message.success(`已触发: ${job.name}`)
      } catch (e) {
        this.$message.error('触发失败: ' + (e.response?.data?.error || e.message))
      } finally {
        this.$set(job, '_triggering', false)
      }
    },

    async handleDelete (job) {
      try {
        await deleteCronJob(job.id)
        this.$message.success('已删除')
        this.fetchJobs()
      } catch (e) {
        this.$message.error('删除失败')
      }
    },

    validateCron (rule, value, callback) {
      if (!value) {
        callback(new Error('请输入 cron 表达式'))
        return
      }
      const parts = value.trim().split(/\s+/)
      if (parts.length !== 5) {
        callback(new Error('需要 5 段：分 时 日 月 周'))
        return
      }
      callback()
    },

    describeCron (expr) {
      if (!expr) return ''
      const descs = {
        '0 18 * * 1-5': '工作日 18:00',
        '30 9 * * 1-5': '工作日 9:30',
        '*/5 9-15 * * 1-5': '工作日盘中每 5 分钟',
        '0 */2 * * *': '每 2 小时',
        '*/1 * * * *': '每分钟'
      }
      return descs[expr] || expr
    }
  }
}
</script>

<style scoped>
.cron-jobs-page {
  padding: 24px;
}

.status-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.status-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.header-icon {
  font-size: 24px;
  color: #1890ff;
}

.header-title {
  font-size: 20px;
  font-weight: 600;
}

.job-count {
  color: #999;
  font-size: 13px;
}

.event-feed {
  max-height: 200px;
  overflow-y: auto;
  margin-bottom: 8px;
}

.event-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-radius: 4px;
  margin-bottom: 4px;
  font-size: 13px;
  animation: slideIn 0.3s ease;
}

.event-start {
  background: #e6f7ff;
  color: #1890ff;
}

.event-success {
  background: #f6ffed;
  color: #52c41a;
}

.event-error {
  background: #fff2f0;
  color: #ff4d4f;
}

.event-time {
  color: #999;
  font-size: 12px;
  min-width: 80px;
}

.event-name {
  font-weight: 500;
  min-width: 120px;
}

.event-msg {
  color: #666;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.text-muted {
  color: #bbb;
}

.cron-hint {
  margin-top: 4px;
  font-size: 12px;
  color: #999;
}

.cron-hint a {
  margin-left: 8px;
  cursor: pointer;
}

.cron-hint a:hover {
  color: #1890ff;
}

@keyframes slideIn {
  from {
    opacity: 0;
    transform: translateY(-8px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.event-slide-enter-active {
  transition: all 0.3s ease;
}

.event-slide-enter {
  opacity: 0;
  transform: translateX(-20px);
}
</style>
