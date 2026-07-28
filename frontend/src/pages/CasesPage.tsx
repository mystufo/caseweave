import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  exportFilteredCases,
  fetchAllCases,
  submitFeedback,
  deleteTestCase,
  type AllCasesItem,
  type TestCase,
} from '../api/client'
import TabBar, { type ViewKey } from '../components/TabBar'
import {
  Search, RefreshCw, ChevronDown, ChevronRight, Loader2, Filter, X, Download,
  Edit2, Check, Trash2, ThumbsUp, ThumbsDown,
} from 'lucide-react'

interface PageProps {
  view: ViewKey
  onChangeView: (v: ViewKey) => void
}

type Priority = 'P1' | 'P2' | 'P3'

const PRIORITIES: Priority[] = ['P1', 'P2', 'P3']
const PRIORITY_STYLE: Record<Priority, string> = {
  P1: 'bg-red-100 text-red-700 border-red-200',
  P2: 'bg-amber-100 text-amber-700 border-amber-200',
  P3: 'bg-gray-100 text-gray-600 border-gray-200',
}

// ── Column config ─────────────────────────────────────────────────────────────
// 单一来源：表头列、默认宽度、最小宽度。表格 `table-layout: fixed`，所以宽度真的会被尊重。
interface ColDef {
  key: string
  label: string
  width: number  // default px
  minWidth: number
  align?: 'left' | 'center'
}

const COLUMNS: ColDef[] = [
  { key: 'case_number', label: '用例编号', width: 140, minWidth: 100 },
  { key: 'name', label: '名称', width: 220, minWidth: 120 },
  { key: 'module', label: '模块', width: 110, minWidth: 80 },
  { key: 'priority', label: '优先级', width: 72, minWidth: 60, align: 'center' },
  { key: 'preconditions', label: '前置条件', width: 180, minWidth: 100 },
  { key: 'steps', label: '执行步骤', width: 300, minWidth: 140 },
  { key: 'expected_result', label: '预期结果', width: 240, minWidth: 140 },
  { key: 'remarks', label: '备注', width: 140, minWidth: 80 },
  { key: 'session', label: '所属会话', width: 160, minWidth: 100 },
  { key: 'created_at', label: '创建时间', width: 130, minWidth: 100 },
  { key: 'actions', label: '操作', width: 150, minWidth: 130, align: 'center' },
]

type EditableField = 'name' | 'priority' | 'preconditions' | 'steps' | 'expected_result' | 'remarks'
type EditDraft = Partial<Pick<TestCase, EditableField>>

const COL_WIDTHS_KEY = 'caseweave.cases.colWidths'

function loadColWidths(): Record<string, number> {
  try {
    const raw = localStorage.getItem(COL_WIDTHS_KEY)
    if (!raw) return Object.fromEntries(COLUMNS.map(c => [c.key, c.width]))
    const parsed = JSON.parse(raw) as Record<string, number>
    // 容错：补全缺失列、过滤掉已不存在的列
    return Object.fromEntries(COLUMNS.map(c => [c.key, Number(parsed[c.key]) || c.width]))
  } catch {
    return Object.fromEntries(COLUMNS.map(c => [c.key, c.width]))
  }
}

function PriorityBadge({ value }: { value: string | undefined }) {
  const v = (PRIORITIES.includes((value || '') as Priority) ? value : 'P2') as Priority
  return (
    <span className={`inline-block px-1.5 py-0.5 rounded border text-[11px] font-mono font-semibold ${PRIORITY_STYLE[v]}`}>
      {v}
    </span>
  )
}

function formatDateShort(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const yy = d.getFullYear()
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mi = String(d.getMinutes()).padStart(2, '0')
  return `${yy}-${mm}-${dd} ${hh}:${mi}`
}

export default function CasesPage({ view, onChangeView }: PageProps) {
  const [cases, setCases] = useState<AllCasesItem[]>([])
  const [loading, setLoading] = useState(false)
  const [query, setQuery] = useState('')
  const [moduleFilter, setModuleFilter] = useState<string>('')  // '' = 全部
  const [priorityFilter, setPriorityFilter] = useState<Set<Priority>>(new Set())
  const [dateFrom, setDateFrom] = useState<string>('')  // YYYY-MM-DD
  const [dateTo, setDateTo] = useState<string>('')
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const [colWidths, setColWidths] = useState<Record<string, number>>(loadColWidths)
  // 编辑/删除状态：editing 行 id；editDraft 该行未保存草稿；
  // confirmingDelete 二次确认（同 TestCaseTable 的设计，避免误删）
  const [editing, setEditing] = useState<number | null>(null)
  const [editDraft, setEditDraft] = useState<EditDraft>({})
  const [savingEdit, setSavingEdit] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState<number | null>(null)
  const [deleting, setDeleting] = useState<number | null>(null)
  // 点赞/点踩：feedback 记住每行已选态；dislikeReasonFor 展开原因输入的行 id
  const [feedback, setFeedback] = useState<Record<number, 'like' | 'dislike'>>({})
  const [dislikeReasonFor, setDislikeReasonFor] = useState<number | null>(null)
  const [dislikeReason, setDislikeReason] = useState('')

  useEffect(() => {
    localStorage.setItem(COL_WIDTHS_KEY, JSON.stringify(colWidths))
  }, [colWidths])

  const load = useCallback(() => {
    setLoading(true)
    fetchAllCases()
      .then(r => setCases(r.cases))
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    if (view !== 'cases') return
    load()
  }, [view, load])

  const moduleOptions = useMemo(() => {
    const s = new Set<string>()
    cases.forEach(c => s.add(c.module || '未分组'))
    return Array.from(s).sort((a, b) => a.localeCompare(b, 'zh'))
  }, [cases])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    // 日期边界：from = 当天 00:00 本地时间；to = 当天 23:59:59.999 本地时间
    const fromTs = dateFrom ? new Date(`${dateFrom}T00:00:00`).getTime() : null
    const toTs = dateTo ? new Date(`${dateTo}T23:59:59.999`).getTime() : null

    return cases.filter(c => {
      // 关键词跨字段模糊：编号 / 名称 / 步骤 / 预期 / 备注 / 会话标题
      if (q) {
        const hay = [
          c.case_number, c.name, c.module, c.session_title,
          c.preconditions, c.steps, c.expected_result, c.remarks,
        ].map(v => (v || '').toLowerCase()).join('\n')
        if (!hay.includes(q)) return false
      }
      // 模块精确匹配
      if (moduleFilter) {
        const m = c.module || '未分组'
        if (m !== moduleFilter) return false
      }
      // 优先级：未勾选任何 = 不过滤；勾选了就必须在集合里
      if (priorityFilter.size > 0) {
        const p = (PRIORITIES.includes((c.priority || '') as Priority) ? c.priority : 'P2') as Priority
        if (!priorityFilter.has(p)) return false
      }
      // 创建时间区间
      if (fromTs != null || toTs != null) {
        if (!c.created_at) return false
        const ts = new Date(c.created_at).getTime()
        if (Number.isNaN(ts)) return false
        if (fromTs != null && ts < fromTs) return false
        if (toTs != null && ts > toTs) return false
      }
      return true
    })
  }, [cases, query, moduleFilter, priorityFilter, dateFrom, dateTo])

  const grouped = useMemo(() => {
    const map = new Map<string, AllCasesItem[]>()
    filtered.forEach(c => {
      const key = c.module || '未分组'
      if (!map.has(key)) map.set(key, [])
      map.get(key)!.push(c)
    })
    return Array.from(map.entries()).sort((a, b) => a[0].localeCompare(b[0], 'zh'))
  }, [filtered])

  const totalSessions = useMemo(
    () => new Set(filtered.map(c => c.session_id)).size,
    [filtered],
  )

  const hasActiveFilter = !!moduleFilter || priorityFilter.size > 0 || !!dateFrom || !!dateTo
  const resetFilters = () => {
    setModuleFilter('')
    setPriorityFilter(new Set())
    setDateFrom('')
    setDateTo('')
  }

  const togglePriority = (p: Priority) => {
    setPriorityFilter(prev => {
      const next = new Set(prev)
      if (next.has(p)) next.delete(p)
      else next.add(p)
      return next
    })
  }

  const [exporting, setExporting] = useState(false)
  const handleExport = useCallback(async () => {
    if (filtered.length === 0 || exporting) return
    setExporting(true)
    try {
      await exportFilteredCases(filtered.map(c => c.id))
    } catch (err) {
      console.error('Export failed:', err)
      alert('导出失败，请稍后重试')
    } finally {
      setExporting(false)
    }
  }, [filtered, exporting])

  // ── 列宽拖拽 ──────────────────────────────────────────────────────────────
  // mousedown 记下起点，document 级别监听 move/up，避免鼠标拖出表头时丢失事件
  const resizingRef = useRef<{ key: string; startX: number; startW: number; minW: number } | null>(null)

  const handleResizeStart = useCallback((e: React.MouseEvent, col: ColDef) => {
    e.preventDefault()
    e.stopPropagation()
    resizingRef.current = {
      key: col.key,
      startX: e.clientX,
      startW: colWidths[col.key] ?? col.width,
      minW: col.minWidth,
    }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    const onMove = (ev: MouseEvent) => {
      const r = resizingRef.current
      if (!r) return
      const w = Math.max(r.minW, r.startW + ev.clientX - r.startX)
      setColWidths(prev => ({ ...prev, [r.key]: w }))
    }
    const onUp = () => {
      resizingRef.current = null
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [colWidths])

  const startEdit = useCallback((c: AllCasesItem) => {
    setConfirmingDelete(null)
    setEditing(c.id)
    setEditDraft({
      name: c.name,
      priority: (PRIORITIES.includes((c.priority || '') as Priority) ? c.priority : 'P2') as Priority,
      preconditions: c.preconditions,
      steps: c.steps,
      expected_result: c.expected_result,
      remarks: c.remarks,
    })
  }, [])

  const cancelEdit = useCallback(() => {
    setEditing(null)
    setEditDraft({})
  }, [])

  const saveEdit = useCallback(async (c: AllCasesItem) => {
    if (savingEdit) return
    const original: Record<string, string> = {
      name: c.name,
      priority: (PRIORITIES.includes((c.priority || '') as Priority) ? c.priority : 'P2'),
      preconditions: c.preconditions || '',
      steps: c.steps || '',
      expected_result: c.expected_result || '',
      remarks: c.remarks || '',
    }
    const modified: Record<string, string> = {
      name: (editDraft.name ?? '').toString(),
      priority: (editDraft.priority ?? 'P2').toString(),
      preconditions: (editDraft.preconditions ?? '').toString(),
      steps: (editDraft.steps ?? '').toString(),
      expected_result: (editDraft.expected_result ?? '').toString(),
      remarks: (editDraft.remarks ?? '').toString(),
    }
    setSavingEdit(true)
    try {
      await submitFeedback(c.id, 'edit', modified, original)
      setCases(prev => prev.map(x => (x.id === c.id ? { ...x, ...editDraft } : x)))
      setEditing(null)
      setEditDraft({})
    } catch (err) {
      console.error('Save edit failed:', err)
      alert('保存失败，请重试')
    } finally {
      setSavingEdit(false)
    }
  }, [editDraft, savingEdit])

  const handleLike = useCallback(async (id: number) => {
    setFeedback(prev => ({ ...prev, [id]: 'like' }))
    try {
      await submitFeedback(id, 'like')
    } catch (err) {
      console.error('Like feedback failed:', err)
    }
  }, [])

  // 点👎先展开可选原因输入，不立即提交；填了原因作强信号进分诊，可直接跳过
  const startDislike = useCallback((id: number) => {
    setEditing(null)
    setConfirmingDelete(null)
    setDislikeReasonFor(id)
    setDislikeReason('')
  }, [])

  const submitDislike = useCallback(async (id: number) => {
    setFeedback(prev => ({ ...prev, [id]: 'dislike' }))
    const reason = dislikeReason.trim()
    setDislikeReasonFor(null)
    setDislikeReason('')
    try {
      await submitFeedback(id, 'dislike', undefined, undefined, reason || undefined)
    } catch (err) {
      console.error('Dislike feedback failed:', err)
    }
  }, [dislikeReason])

  const performDelete = useCallback(async (id: number) => {
    setDeleting(id)
    try {
      await deleteTestCase(id)
      setCases(prev => prev.filter(c => c.id !== id))
    } catch (err) {
      console.error('Delete case failed:', err)
      alert('删除失败，请重试')
    } finally {
      setDeleting(null)
      setConfirmingDelete(null)
    }
  }, [])

  const renderCell = (col: ColDef, c: AllCasesItem) => {
    const isEditing = editing === c.id
    switch (col.key) {
      case 'case_number':
        return <span className="font-mono text-[11px] text-gray-700">{c.case_number}</span>
      case 'name':
        return isEditing ? (
          <input
            className="w-full border rounded px-1 py-0.5 text-xs"
            value={editDraft.name ?? ''}
            onChange={e => setEditDraft(p => ({ ...p, name: e.target.value }))}
          />
        ) : (
          <span className="text-gray-800">{c.name}</span>
        )
      case 'module':
        return <span className="text-gray-500">{c.module || '未分组'}</span>
      case 'priority':
        return isEditing ? (
          <select
            className="border rounded px-1 py-0.5 text-xs"
            value={editDraft.priority ?? 'P2'}
            onChange={e => setEditDraft(p => ({ ...p, priority: e.target.value as Priority }))}
          >
            <option value="P1">P1</option>
            <option value="P2">P2</option>
            <option value="P3">P3</option>
          </select>
        ) : (
          <PriorityBadge value={c.priority} />
        )
      case 'preconditions':
        return isEditing ? (
          <textarea
            className="w-full border rounded px-1 py-0.5 text-xs resize-y"
            rows={3}
            value={editDraft.preconditions ?? ''}
            onChange={e => setEditDraft(p => ({ ...p, preconditions: e.target.value }))}
          />
        ) : (
          <span className="text-gray-600 whitespace-pre-line break-words">{c.preconditions || '—'}</span>
        )
      case 'steps':
        return isEditing ? (
          <textarea
            className="w-full border rounded px-1 py-0.5 text-xs resize-y"
            rows={4}
            value={editDraft.steps ?? ''}
            onChange={e => setEditDraft(p => ({ ...p, steps: e.target.value }))}
          />
        ) : (
          <span className="text-gray-700 whitespace-pre-line break-words">{c.steps || '—'}</span>
        )
      case 'expected_result':
        return isEditing ? (
          <textarea
            className="w-full border rounded px-1 py-0.5 text-xs resize-y"
            rows={4}
            value={editDraft.expected_result ?? ''}
            onChange={e => setEditDraft(p => ({ ...p, expected_result: e.target.value }))}
          />
        ) : (
          <span className="text-gray-700 whitespace-pre-line break-words">{c.expected_result || '—'}</span>
        )
      case 'remarks':
        return isEditing ? (
          <textarea
            className="w-full border rounded px-1 py-0.5 text-xs resize-y"
            rows={2}
            value={editDraft.remarks ?? ''}
            onChange={e => setEditDraft(p => ({ ...p, remarks: e.target.value }))}
          />
        ) : (
          <span className="text-gray-500 whitespace-pre-line break-words">{c.remarks || '—'}</span>
        )
      case 'session':
        return (
          <div className="text-gray-500 truncate" title={c.session_title}>
            {c.session_title}
            <span className="text-gray-300 ml-1">#{c.session_id}</span>
          </div>
        )
      case 'created_at':
        return <span className="text-gray-500 font-mono text-[11px]">{formatDateShort(c.created_at)}</span>
      case 'actions':
        if (isEditing) {
          return (
            <div className="flex items-center justify-center gap-1">
              <button
                onClick={() => saveEdit(c)}
                disabled={savingEdit}
                className="text-green-600 hover:text-green-700 disabled:opacity-50"
                title="保存"
              >
                {savingEdit ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
              </button>
              <button
                onClick={cancelEdit}
                disabled={savingEdit}
                className="text-red-400 hover:text-red-500 disabled:opacity-50"
                title="取消"
              >
                <X size={14} />
              </button>
            </div>
          )
        }
        if (confirmingDelete === c.id) {
          return (
            <div className="flex items-center justify-center gap-1">
              <span className="text-[11px] text-red-600">确认删除？</span>
              <button
                onClick={() => performDelete(c.id)}
                disabled={deleting === c.id}
                className="text-red-600 hover:text-red-700 disabled:opacity-50"
                title="确认删除"
              >
                {deleting === c.id ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
              </button>
              <button
                onClick={() => setConfirmingDelete(null)}
                disabled={deleting === c.id}
                className="text-gray-400 hover:text-gray-500 disabled:opacity-50"
                title="取消"
              >
                <X size={14} />
              </button>
            </div>
          )
        }
        if (dislikeReasonFor === c.id) {
          return (
            <div className="flex items-center justify-center gap-1">
              <input
                type="text"
                value={dislikeReason}
                onChange={e => setDislikeReason(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') void submitDislike(c.id) }}
                placeholder="原因（可选）"
                autoFocus
                className="w-24 px-1.5 py-0.5 text-[11px] border border-gray-200 rounded focus:outline-none focus:ring-1 focus:ring-red-300"
              />
              <button
                onClick={() => void submitDislike(c.id)}
                className="text-red-500 hover:text-red-600"
                title="提交（可留空）"
              >
                <Check size={14} />
              </button>
              <button
                onClick={() => { setDislikeReasonFor(null); setDislikeReason('') }}
                className="text-gray-400 hover:text-gray-500"
                title="取消"
              >
                <X size={14} />
              </button>
            </div>
          )
        }
        return (
          <div className="flex items-center justify-center gap-1.5">
            <button
              onClick={() => void handleLike(c.id)}
              className={`transition-colors ${feedback[c.id] === 'like' ? 'text-green-600' : 'text-gray-300 hover:text-green-500'}`}
              title="赞"
            >
              <ThumbsUp size={13} />
            </button>
            <button
              onClick={() => startDislike(c.id)}
              className={`transition-colors ${feedback[c.id] === 'dislike' ? 'text-red-500' : 'text-gray-300 hover:text-red-400'}`}
              title="踩（可补充原因，帮助系统改进）"
            >
              <ThumbsDown size={13} />
            </button>
            <button
              onClick={() => startEdit(c)}
              className="text-gray-300 hover:text-blue-500 transition-colors"
              title="编辑"
            >
              <Edit2 size={13} />
            </button>
            <button
              onClick={() => { cancelEdit(); setConfirmingDelete(c.id) }}
              className="text-gray-300 hover:text-red-500 transition-colors"
              title="删除"
            >
              <Trash2 size={13} />
            </button>
          </div>
        )
      default:
        return null
    }
  }

  return (
    <div className="flex h-full bg-gray-50 overflow-hidden">
      <TabBar value={view} onChange={onChangeView} />

      <aside className="w-64 bg-white border-r border-gray-200 flex-shrink-0 flex flex-col">
        <div className="px-4 py-3 border-b border-gray-100">
          <h3 className="text-sm font-semibold text-gray-900">用例管理</h3>
          <p className="text-[11px] text-gray-400 mt-0.5">跨会话聚合视图</p>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3 text-xs text-gray-500 space-y-2">
          <p>聚合所有会话生成的测试用例，按模块分组。</p>
          <ul className="list-disc pl-4 space-y-1 text-gray-400">
            <li>支持按模块 / 优先级 / 创建时间筛选</li>
            <li>拖拽表头右边缘可调整列宽（自动记住）</li>
            <li>支持就地编辑用例字段、单条删除（二次确认）</li>
            <li>可对用例点赞 / 点踩（踩可补充原因），驱动系统进化</li>
            <li>点击「导出」下载当前筛选结果的 Excel</li>
          </ul>
        </div>
      </aside>

      <main className="flex-1 min-w-0 flex flex-col bg-gray-50">
        {/* Header */}
        <div className="px-6 py-4 border-b border-gray-200 bg-white space-y-3">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-gray-900">用例管理</h2>
              <p className="text-xs text-gray-500 mt-0.5">
                共 <span className="font-medium text-gray-800">{filtered.length}</span> 条用例，
                来自 <span className="font-medium text-gray-800">{totalSessions}</span> 个会话，
                <span className="font-medium text-gray-800">{grouped.length}</span> 个模块
                {hasActiveFilter && (
                  <span className="text-amber-600 ml-2">· 已筛选</span>
                )}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={handleExport}
                disabled={exporting || filtered.length === 0}
                className="flex items-center gap-1.5 text-xs text-white bg-green-600 hover:bg-green-700 disabled:opacity-40 disabled:cursor-not-allowed px-3 py-1.5 rounded-lg transition-colors"
                title={filtered.length === 0 ? '当前没有可导出的用例' : `导出当前 ${filtered.length} 条用例为 Excel`}
              >
                {exporting ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                导出 Excel
              </button>
              <button
                onClick={load}
                disabled={loading}
                className="flex items-center gap-1.5 text-xs text-gray-600 px-2.5 py-1.5 border border-gray-200 rounded-lg hover:bg-gray-50 disabled:opacity-50"
              >
                {loading ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
                刷新
              </button>
            </div>
          </div>

          {/* 搜索 + 筛选行 */}
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative flex-1 min-w-[200px]">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
              <input
                value={query}
                onChange={e => setQuery(e.target.value)}
                placeholder="搜索：用例编号 / 名称 / 步骤 / 预期 / 备注 / 会话"
                className="w-full pl-8 pr-3 py-1.5 text-sm border border-gray-200 rounded-lg outline-none focus:border-amber-400"
              />
            </div>

            {/* 模块下拉 */}
            <label className="flex items-center gap-1 text-xs text-gray-600">
              <Filter size={12} className="text-gray-400" />
              模块
              <select
                value={moduleFilter}
                onChange={e => setModuleFilter(e.target.value)}
                className="border border-gray-200 rounded-md px-2 py-1 text-xs bg-white outline-none focus:border-amber-400 max-w-[160px]"
              >
                <option value="">全部</option>
                {moduleOptions.map(m => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </label>

            {/* 优先级 chips */}
            <div className="flex items-center gap-1 text-xs text-gray-600">
              优先级
              {PRIORITIES.map(p => {
                const on = priorityFilter.has(p)
                return (
                  <button
                    key={p}
                    type="button"
                    onClick={() => togglePriority(p)}
                    className={`px-2 py-0.5 rounded border font-mono text-[11px] font-semibold transition-colors ${
                      on
                        ? PRIORITY_STYLE[p]
                        : 'bg-white text-gray-400 border-gray-200 hover:border-gray-300'
                    }`}
                  >
                    {p}
                  </button>
                )
              })}
            </div>

            {/* 创建时间区间 */}
            <label className="flex items-center gap-1 text-xs text-gray-600">
              创建时间
              <input
                type="date"
                value={dateFrom}
                onChange={e => setDateFrom(e.target.value)}
                className="border border-gray-200 rounded-md px-2 py-1 text-xs bg-white outline-none focus:border-amber-400"
              />
              <span className="text-gray-400">→</span>
              <input
                type="date"
                value={dateTo}
                onChange={e => setDateTo(e.target.value)}
                className="border border-gray-200 rounded-md px-2 py-1 text-xs bg-white outline-none focus:border-amber-400"
              />
            </label>

            {hasActiveFilter && (
              <button
                type="button"
                onClick={resetFilters}
                className="flex items-center gap-1 text-xs text-gray-500 px-2 py-1 border border-gray-200 rounded-md hover:bg-gray-50"
              >
                <X size={12} />
                清空筛选
              </button>
            )}
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {grouped.length === 0 && !loading && (
            <div className="text-center text-sm text-gray-400 py-16">
              {cases.length === 0
                ? '暂无用例。请在「对话」中上传需求文档生成。'
                : '当前筛选条件下没有匹配的用例。'}
            </div>
          )}

          {grouped.map(([module, items]) => {
            const isCollapsed = collapsed[module]
            return (
              <div key={module} className="bg-white border border-gray-200 rounded-xl overflow-hidden">
                <button
                  type="button"
                  onClick={() => setCollapsed(prev => ({ ...prev, [module]: !prev[module] }))}
                  className="w-full flex items-center justify-between gap-2 px-4 py-2.5 hover:bg-gray-50"
                >
                  <div className="flex items-center gap-2">
                    {isCollapsed
                      ? <ChevronRight size={14} className="text-gray-400" />
                      : <ChevronDown size={14} className="text-gray-400" />}
                    <span className="text-sm font-medium text-gray-900">{module}</span>
                    <span className="text-xs text-gray-400">{items.length} 条</span>
                  </div>
                </button>

                {!isCollapsed && (
                  <div className="overflow-x-auto border-t border-gray-100">
                    <table className="text-xs" style={{ tableLayout: 'fixed', width: 'max-content', minWidth: '100%' }}>
                      <colgroup>
                        {COLUMNS.map(col => (
                          <col key={col.key} style={{ width: `${colWidths[col.key] ?? col.width}px` }} />
                        ))}
                      </colgroup>
                      <thead className="bg-gray-50 text-gray-500 sticky top-0 z-10">
                        <tr>
                          {COLUMNS.map((col, colIdx) => (
                            <th
                              key={col.key}
                              className={`font-medium px-3 py-2 select-none border-b border-gray-200 ${
                                col.align === 'center' ? 'text-center' : 'text-left'
                              }`}
                              style={{ position: 'relative' }}
                            >
                              <span className="block truncate">{col.label}</span>
                              {/* 列宽拖拽把手：常驻一条深色竖线，hover 变黑加粗 */}
                              {colIdx < COLUMNS.length - 1 && (
                                <span
                                  onMouseDown={(e) => handleResizeStart(e, col)}
                                  title="拖拽调整列宽"
                                  className="group absolute top-0 right-0 h-full w-2 cursor-col-resize flex justify-center items-stretch"
                                  style={{ touchAction: 'none' }}
                                >
                                  <span className="block w-px h-full bg-gray-800/70 group-hover:w-[2px] group-hover:bg-black group-active:bg-amber-500" />
                                </span>
                              )}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {items.map(c => (
                          <tr key={c.id} className="border-t border-gray-100 hover:bg-gray-50/60 align-top">
                            {COLUMNS.map(col => (
                              <td
                                key={col.key}
                                className={`px-3 py-2 ${col.align === 'center' ? 'text-center' : ''}`}
                                style={{ wordBreak: 'break-word', overflowWrap: 'anywhere' }}
                              >
                                {renderCell(col, c)}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </main>
    </div>
  )
}
