<template>
  <section class="page" data-module="reserve">
    <header class="page-head">
      <div>
        <h2>储量估算管理</h2>
        <p class="page-desc">
          块段边界、品位区间、封边规则统一按现行口径（V2）判定；审批模型与手工公式冲突时按审批优先级裁决。
          面积 ≤ {{ limits.maxArea }} ㎡、品位 ≤ {{ limits.maxGrade }}% 方可保存；历史项目维持 V1 旧版本。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记矿体块段</button>
        <button class="btn" type="button" @click="runRecalculate">按新口径重算</button>
        <button class="btn" type="button" @click="runMigration">迁移历史面积快照</button>
        <button class="btn" type="button" @click="exportRows">导出储量估算清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div class="consistency-bar" :class="consistency.ok ? 'ok' : 'bad'">
      <span>
        三链路一致性（块段台账 × 估算清单 × 储量图）：
        <strong>{{ consistency.ok ? '一致' : `存在 ${consistency.inconsistent} 处不一致` }}</strong>
        · 已发布 {{ consistency.published }} 个结论
      </span>
      <button class="link" type="button" @click="reloadConsistency">重新校验</button>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>块段编号</span>
        <input v-model="keyword" placeholder="按块段编号检索" />
      </label>
      <label class="filter-item">
        <span>块段状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table reserve-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">
            <template v-if="column === '块段状态'">
              <span :class="['status-tag', statusClass(row[column])]">{{ row[column] ?? '—' }}</span>
              <em v-if="draftRowIds.has(Number(row.id))" class="draft-flag">草稿</em>
            </template>
            <template v-else-if="column === '结论来源'">
              {{ sourceLabel(row[column]) }}
              <span v-if="row['formula_version'] === 'v1'" class="version-tag v1">V1历史</span>
            </template>
            <template v-else>{{ formatCell(column, row[column]) }}</template>
          </td>
          <td class="row-actions">
            <button
              v-if="row['块段状态'] === '待估算' || row['块段状态'] === '已估算'"
              class="link" type="button" @click="openEstimate(row)"
            >
              完成估算
            </button>
            <button
              v-if="row['块段状态'] === '已估算' || row['块段状态'] === '待评审'"
              class="link" type="button" @click="runAction('提交评审', row)"
            >
              提交评审
            </button>
            <button
              v-if="draftRowIds.has(Number(row.id)) || row['块段状态'] === '待评审'"
              class="link primary-link" type="button" @click="runAction('认定结果', row)"
            >
              认定发布
            </button>
            <button class="link" type="button" @click="openDetail(row)">估算详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无储量估算数据，可先登记矿体块段</td>
        </tr>
      </tbody>
    </table>

    <!-- 储量图投影（与台账同结论派生） -->
    <section class="map-panel">
      <h3>储量图投影（线型 = 封边规则，底色 = 品位区间）</h3>
      <div class="map-legend">
        <span><i class="line solid" />全封边</span>
        <span><i class="line dashed" />半封边</span>
        <span><i class="line dotted" />不封边</span>
        <span><i class="swatch" style="background:#2563eb" />高品位</span>
        <span><i class="swatch" style="background:#93c5fd" />中品位</span>
        <span><i class="swatch" style="background:#dbeafe" />低品位</span>
      </div>
      <div class="map-grid">
        <article
          v-for="item in mapRows" :key="String(item.id)"
          class="map-cell"
          :style="{ backgroundColor: String(item.fill), borderStyle: String(item.stroke) }"
        >
          <strong>{{ item['块段编号'] }}</strong>
          <span>{{ item['矿体名称'] }} · {{ item['块段边界'] }}</span>
          <span>{{ item['品位区间'] }} · {{ item['封边规则'] }}</span>
          <span class="map-cid">{{ item['结论编号'] || '草稿' }}</span>
        </article>
        <p v-if="!mapRows.length" class="empty-state">暂无已发布图元</p>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条储量估算记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-if="infoMessage" class="info-text">{{ infoMessage }}</span>
    </footer>

    <!-- 登记弹窗 -->
    <div v-if="createOpen" class="modal-mask" @click.self="createOpen = false">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记矿体块段（现行 V2 口径）</h3>
        <label v-for="f in createFields" :key="f.key" class="modal-field">
          <span>{{ f.label }}<i v-if="f.required">*</i></span>
          <input v-model="createForm[f.key]" :type="f.type || 'text'" :placeholder="f.placeholder" />
        </label>
        <p v-if="createError" class="error-text">{{ createError }}</p>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="createOpen = false">取消</button>
          <button class="btn primary" type="submit">保存</button>
        </div>
      </form>
    </div>

    <!-- 完成估算弹窗 -->
    <div v-if="estimateOpen" class="modal-mask" @click.self="estimateOpen = false">
      <form class="modal wide" @submit.prevent="submitEstimate">
        <h3>完成估算 · {{ estimateTarget?.['块段编号'] }}</h3>
        <p class="modal-hint">
          手工公式入参为必填；审批模型入参留空时与手工一致。两套结论冲突时一律按审批优先级裁决。
        </p>
        <div class="estimate-columns">
          <fieldset>
            <legend>手工公式</legend>
            <label v-for="f in estimateFields" :key="'m-' + f.key" class="modal-field">
              <span>{{ f.label }}</span>
              <input v-model.number="manualForm[f.key]" type="number" step="any" />
            </label>
          </fieldset>
          <fieldset>
            <legend>审批模型（可只覆盖差异项）</legend>
            <label v-for="f in estimateFields" :key="'a-' + f.key" class="modal-field">
              <span>{{ f.label }}</span>
              <input v-model.number="approvalForm[f.key]" type="number" step="any" :placeholder="'留空同手工'" />
            </label>
          </fieldset>
        </div>
        <p v-if="estimateError" class="error-text">{{ estimateError }}</p>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="estimateOpen = false">取消</button>
          <button class="btn primary" type="submit">保存草稿</button>
        </div>
      </form>
    </div>

    <!-- 估算详情抽屉 -->
    <div v-if="detail" class="modal-mask" @click.self="detail = null">
      <div class="modal wide detail-modal">
        <h3>估算详情 · {{ detail['块段编号'] }}</h3>
        <p class="modal-hint">
          公式版本：{{ detail.formula_version === 'v1' ? 'V1（历史冻结）' : 'V2（现行统一口径）' }}
          · 面积快照：{{ detail['面积快照'] ?? detail['面积'] }}
          <template v-if="detail['迁移来源']"> · 快照迁移来源：{{ detail['迁移来源'] }}</template>
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th>来源</th><th>状态</th><th>估算时间</th><th>面积</th><th>厚度</th><th>品位</th>
              <th>矿石体重</th><th>块段边界</th><th>品位区间</th><th>封边规则</th><th>矿石量</th><th>金属量</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(e, idx) in detailRows" :key="idx" :class="{ winning: e.winning_source && e['状态'] === '草稿' && isWinnerRow(e, idx) }">
              <td>{{ e['来源'] }}</td><td>{{ e['状态'] }}</td><td>{{ e['估算时间'] }}</td>
              <td>{{ e['面积'] }}</td><td>{{ e['厚度'] }}</td><td>{{ e['品位'] }}</td>
              <td>{{ e['矿石体重'] }}</td><td>{{ e['块段边界'] }}</td><td>{{ e['品位区间'] }}</td>
              <td>{{ e['封边规则'] }}</td><td>{{ e['矿石量'] }}</td><td>{{ e['金属量'] }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="detailConflicts.length" class="conflict-box">
          <strong>冲突裁决记录（按审批优先级）：</strong>
          <ul>
            <li v-for="(c, i) in detailConflicts" :key="i">
              {{ c.field }}：审批 {{ c.winner_value }} 采用，手工 {{ c.loser_value }} 被覆盖
            </li>
          </ul>
        </div>
        <div class="modal-actions">
          <button class="btn primary" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/reserve'
const columns = [
  '块段编号', '矿体名称', '项目名称', '面积', '厚度', '品位', '矿石体重',
  '块段边界', '品位区间', '封边规则', '矿石量', '金属量', '结论来源', '块段状态',
]
const statuses = ['待估算', '已估算', '待评审', '已认定']
const limits = { maxArea: '1,000,000', maxGrade: '20' }

type Row = Record<string, string | number | boolean | null>
interface Consistency { ok: boolean; published: number; inconsistent: number; rows: unknown[] }

const rows = ref<Row[]>([])
const mapRows = ref<Row[]>([])
const drafts = ref<Row[]>([])
const total = ref(0)
const keyword = ref('')
const statusFilter = ref('')
const errorMessage = ref('')
const infoMessage = ref('')
const consistency = ref<Consistency>({ ok: true, published: 0, inconsistent: 0, rows: [] })

const stats = computed(() => [
  { label: '块段总数', value: total.value },
  { label: '待估算块段', value: rows.value.filter(r => r['块段状态'] === '待估算').length },
  { label: '待评审块段', value: rows.value.filter(r => r['块段状态'] === '待评审').length },
  { label: '已认定块段', value: rows.value.filter(r => r['块段状态'] === '已认定').length },
])

const draftRowIds = computed(() => new Set(drafts.value.map(d => Number(d.id))))

// ---- 弹窗状态 ----
const createOpen = ref(false)
const createError = ref('')
const createFields = [
  { key: '块段编号', label: '块段编号', required: true },
  { key: '矿体名称', label: '矿体名称', required: true },
  { key: '项目名称', label: '项目名称' },
  { key: '面积', label: '面积（㎡，≤100万）', required: true, type: 'number' },
  { key: '厚度', label: '厚度（m）', type: 'number' },
  { key: '品位', label: '品位（%，≤20）', required: true, type: 'number' },
  { key: '矿石体重', label: '矿石体重（t/m³）', type: 'number' },
  { key: '资源类别', label: '资源类别', placeholder: '推断的/控制的/探明的' },
]
const createForm = ref<Record<string, string | number>>({})

const estimateOpen = ref(false)
const estimateError = ref('')
const estimateTarget = ref<Row | null>(null)
const estimateFields = [
  { key: 'area', label: '面积（㎡）' },
  { key: 'thickness', label: '厚度（m）' },
  { key: 'grade', label: '品位（%）' },
  { key: 'density', label: '矿石体重（t/m³）' },
]
const manualForm = ref<Record<string, number>>({ area: 0, thickness: 0, grade: 0, density: 0 })
const approvalForm = ref<Record<string, number>>({ area: 0, thickness: 0, grade: 0, density: 0 })

const detail = ref<Record<string, unknown> | null>(null)
const detailRows = computed<Row[]>(() => {
  const d = detail.value?.['估算详情'] as { 正式结论?: Row[]; 草稿结论?: Row[] } | undefined
  return [...(d?.正式结论 ?? []), ...(d?.草稿结论 ?? [])]
})
const detailConflicts = computed(() =>
  detailRows.value.flatMap(r => (r['冲突明细'] as unknown as Array<Record<string, string>>) ?? []),
)

function sourceLabel(value: unknown): string {
  if (value === 'approval') return '审批模型'
  if (value === 'manual') return '手工公式'
  return value ? String(value) : '—'
}

function formatCell(column: string, value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (['面积', '矿石量'].includes(column) && typeof value === 'number') return value.toLocaleString()
  return String(value)
}

function statusClass(status: unknown): string {
  return { 待估算: 'st-draft', 已估算: 'st-est', 待评审: 'st-review', 已认定: 'st-done' }[String(status)] ?? ''
}

function isWinnerRow(row: Row, idx: number): boolean {
  const list = detailRows.value
  // 同批草稿最后一条是裁决赢家行（后端打 _winning 标记会在序列化后保留）。
  return Boolean(row._winning) || (idx === list.length - 1 && row['状态'] === '草稿')
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  createForm.value = {}
  createError.value = ''
  createOpen.value = true
}

async function submitCreate() {
  createError.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: createForm.value }),
    })
    if (response.status === 400) {
      createError.value = (await response.json()).detail
      return
    }
    const payload = await response.json()
    if (!payload.ok) {
      createError.value = payload.message
      return
    }
    createOpen.value = false
    infoMessage.value = payload.message
    await reload()
  } catch (error) {
    createError.value = error instanceof Error ? error.message : '登记失败'
  }
}

function openEstimate(row: Row) {
  estimateTarget.value = row
  estimateError.value = ''
  manualForm.value = {
    area: Number(row['面积']) || 0,
    thickness: Number(row['厚度']) || 0,
    grade: Number(row['品位']) || 0,
    density: Number(row['矿石体重']) || 0,
  }
  approvalForm.value = { area: 0, thickness: 0, grade: 0, density: 0 }
  estimateOpen.value = true
}

async function submitEstimate() {
  if (!estimateTarget.value) return
  estimateError.value = ''
  const values: Record<string, number> = {
    面积: manualForm.value.area,
    厚度: manualForm.value.thickness,
    品位: manualForm.value.grade,
    矿石体重: manualForm.value.density,
  }
  for (const f of estimateFields) {
    const v = approvalForm.value[f.key]
    if (v !== 0 && v !== null && v !== undefined && !Number.isNaN(v)) {
      values[`approval_${ { area: '面积', thickness: '厚度', grade: '品位', density: '矿石体重' }[f.key] }`] = v
    }
  }
  try {
    const response = await request(`${ENDPOINT}/${estimateTarget.value.id}/estimate`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      estimateError.value = payload.detail || payload.message
      return
    }
    estimateOpen.value = false
    infoMessage.value = payload.message
    await Promise.all([reload(), reloadDrafts()])
  } catch (error) {
    estimateError.value = error instanceof Error ? error.message : '估算失败'
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) throw new Error('估算详情读取失败')
    detail.value = (await response.json()) as Row
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '估算详情读取失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message
      return
    }
    infoMessage.value = payload.message
    await Promise.all([reload(), reloadDrafts(), reloadConsistency(), reloadMap()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '储量估算操作失败'
  }
}

async function runRecalculate() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/recalculate`, {
      method: 'POST',
      body: JSON.stringify({ values: {} }),
    })
    const payload = await response.json()
    const r = payload.entry
    infoMessage.value = `重算完成：刷新草稿 ${r.recalculated.length} 个、重新发布 ${r.republished.length} 个、历史跳过 ${r.skipped_historical.length} 个`
    await Promise.all([reload(), reloadDrafts(), reloadConsistency(), reloadMap()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重算失败'
  }
}

async function runMigration() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/migrate-area-snapshots`, {
      method: 'POST',
      body: JSON.stringify({ values: {} }),
    })
    const payload = await response.json()
    infoMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) throw new Error('矿体块段列表读取失败')
    const payload = await response.json()
    rows.value = (payload.items ?? []) as Row[]
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '储量估算列表读取失败'
  }
}

async function reloadDrafts() {
  const payload = await (await request(`${ENDPOINT}/drafts`)).json()
  drafts.value = payload.items ?? []
}

async function reloadMap() {
  const payload = await (await request(`${ENDPOINT}/map`)).json()
  mapRows.value = payload.items ?? []
}

async function reloadConsistency() {
  consistency.value = await (await request(`${ENDPOINT}/consistency`)).json()
}

onMounted(async () => {
  await Promise.all([reload(), reloadDrafts(), reloadMap(), reloadConsistency()])
})
</script>

<style scoped>
.reserve-table { font-size: 12px; }
.reserve-table th, .reserve-table td { white-space: nowrap; }
.consistency-bar {
  display: flex; justify-content: space-between; align-items: center;
  margin: 10px 0; padding: 8px 12px; border-radius: 6px; font-size: 13px;
}
.consistency-bar.ok { background: #ecfdf5; border: 1px solid #10b981; color: #065f46; }
.consistency-bar.bad { background: #fef2f2; border: 1px solid #ef4444; color: #991b1b; }
.status-tag { padding: 1px 8px; border-radius: 10px; font-style: normal; font-size: 12px; }
.st-draft { background: #f3f4f6; color: #4b5563; }
.st-est { background: #fef9c3; color: #854d0e; }
.st-review { background: #dbeafe; color: #1e40af; }
.st-done { background: #dcfce7; color: #166534; }
.draft-flag { margin-left: 6px; font-style: normal; font-size: 11px; color: #b45309; }
.version-tag { margin-left: 6px; padding: 0 6px; border-radius: 8px; font-size: 11px; font-style: normal; }
.version-tag.v1 { background: #f3e8ff; color: #6b21a8; }
.primary-link { color: var(--brand); font-weight: 600; }
.map-panel { margin-top: 18px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.map-panel h3 { margin: 0 0 10px; font-size: 14px; }
.map-legend { display: flex; gap: 16px; font-size: 12px; color: #4b5563; margin-bottom: 10px; align-items: center; }
.map-legend .line { display: inline-block; width: 22px; height: 0; border-top: 3px #374151; margin-right: 4px; vertical-align: middle; }
.map-legend .line.solid { border-top-style: solid; }
.map-legend .line.dashed { border-top-style: dashed; }
.map-legend .line.dotted { border-top-style: dotted; }
.map-legend .swatch { display: inline-block; width: 12px; height: 12px; border: 1px solid #94a3b8; margin-right: 4px; vertical-align: middle; }
.map-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; }
.map-cell { display: flex; flex-direction: column; gap: 2px; padding: 10px; border: 3px #374151; border-radius: 6px; font-size: 12px; }
.map-cid { color: #6b7280; font-size: 11px; }
.modal-mask {
  position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45);
  display: flex; align-items: center; justify-content: center; z-index: 50;
}
.modal {
  background: #fff; border-radius: 10px; padding: 18px 20px; width: 420px;
  max-height: 86vh; overflow: auto; box-shadow: 0 12px 32px rgba(0, 0, 0, 0.2);
}
.modal.wide { width: 760px; }
.modal h3 { margin: 0 0 10px; font-size: 15px; }
.modal-hint { font-size: 12px; color: #6b7280; margin: 0 0 12px; }
.modal-field { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; font-size: 13px; }
.modal-field i { color: #ef4444; font-style: normal; margin-left: 2px; }
.modal-field input, .modal-field select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
.estimate-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.estimate-columns fieldset { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.estimate-columns legend { padding: 0 6px; font-size: 12px; color: #374151; }
.detail-modal table { font-size: 11px; }
.detail-modal tr.winning { background: #eff6ff; }
.conflict-box { margin-top: 12px; padding: 10px; background: #fffbeb; border: 1px solid #f59e0b; border-radius: 6px; font-size: 12px; }
.conflict-box ul { margin: 6px 0 0; padding-left: 18px; }
.info-text { color: #166534; }
</style>
