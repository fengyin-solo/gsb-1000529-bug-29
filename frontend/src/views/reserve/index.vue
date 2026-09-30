<template>
  <section class="page" data-module="reserve">
    <header class="page-head">
      <div>
        <h2>储量估算管理</h2>
        <p class="page-desc">块段边界、品位区间与封边规则使用同一套审批优先口径，结论同步台账、估算清单与储量图。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记矿体块段</button>
        <button class="btn" type="button" @click="exportRows">导出储量估算清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>口径版本</th>
          <th>校验码</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ formatValue(row[column]) }}</td>
          <td>{{ formatValue(row.formulaVersion) }}</td>
          <td>{{ formatValue(row.conclusionChecksum) }}</td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无储量估算数据，可先登记矿体块段</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条储量估算记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null | Record<string, unknown>>

const ENDPOINT = '/api/reserve'
const columns = ['块段编号', '矿体名称', '面积', '面积快照', '厚度', '品位', '矿石体重', '资源类别', '块段边界', '品位区间', '封边规则', '块段状态']
const statusActions: Record<string, string[]> = {
  待估算: ['完成估算'],
  已估算: ['完成估算', '提交评审'],
  待评审: ['认定结果'],
  已认定: [],
}
const stats = [
  { label: '待估算块段', value: 0 },
  { label: '待评审块段', value: 0 },
  { label: '已认定块段', value: 0 },
]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = ['块段编号', '矿体名称', '块段状态']

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '矿体块段登记入口尚未接入审批流'
}

function formatValue(value: unknown) {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function availableActions(row: Row) {
  return statusActions[String(row.status ?? row['块段状态'] ?? '待估算')] ?? ['完成估算']
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message ?? '储量估算动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '储量估算操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('矿体块段列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats[0].value = rows.value.filter(row => row.status === '待估算').length
    stats[1].value = rows.value.filter(row => row.status === '待评审').length
    stats[2].value = rows.value.filter(row => row.status === '已认定').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '储量估算列表读取失败'
  }
}

onMounted(reload)
</script>
