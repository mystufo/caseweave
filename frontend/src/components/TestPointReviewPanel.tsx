import { useState } from 'react'
import { ListChecks, Plus, Trash2, ArrowUp, ArrowDown, Loader2, RefreshCw, Sparkles, Square } from 'lucide-react'
import type { TestPoint } from '../api/client'

// 两阶段生成的「确认功能点」面板：阶段 1 识别出的功能点清单在这里给用户过目、增删改、调顺序，
// 确认后才按功能点分批写用例。每个功能点 ≈ 3~6 条用例，删掉不想测的、补上漏掉的，
// 用例数量就由这份清单决定，而不是由模型的输出习惯决定。
//
// 编辑副本模式（同 KnowledgeDraftReviewPanel）：props.points 是后端给的原稿，本地 edited 是用户
// 改动后的版本；原稿变化（重新识别 / 刷新恢复）时重置副本。
interface Props {
  points: TestPoint[]
  loading: boolean            // 阶段 1 识别中：只显示 loader + 停止
  submitting: boolean
  moduleName: string
  casePrefix: string
  batchSize: number           // 每批功能点数，用来估算批次数
  onConfirm: (points: TestPoint[]) => void
  onReextract: () => void     // 丢掉当前清单，让模型重新识别
  onCancel?: () => void       // 识别中的「停止」
}

const PRIORITIES: TestPoint['priority'][] = ['P1', 'P2', 'P3']

// 与后端 normalize 同口径的轻量规整：大写、非法字符换成 -、合并连续 -、去首尾 -。
// 只在失焦时做，避免用户打字时光标乱跳；唯一性 / 剥前缀交给后端。
const tidySub = (raw: string) =>
  raw.toUpperCase().replace(/[^A-Z0-9-]+/g, '-').replace(/-{2,}/g, '-').replace(/^-|-$/g, '').slice(0, 20)

export default function TestPointReviewPanel({
  points, loading, submitting, moduleName, casePrefix, batchSize, onConfirm, onReextract, onCancel,
}: Props) {
  const [edited, setEdited] = useState<TestPoint[]>(() => points.map(p => ({ ...p })))
  // 原稿引用变化（重新识别 / 刷新恢复）→ 渲染期重置副本（React 推荐的 derive-from-props 写法）
  const [prevPoints, setPrevPoints] = useState(points)
  if (prevPoints !== points) {
    setPrevPoints(points)
    setEdited(points.map(p => ({ ...p })))
  }

  const busy = loading || submitting
  const valid = edited.filter(p => p.feature.trim())
  const batches = Math.max(1, Math.ceil(valid.length / Math.max(1, batchSize)))
  const hasEmptyFeature = edited.some(p => !p.feature.trim())

  const update = (i: number, patch: Partial<TestPoint>) =>
    setEdited(prev => prev.map((p, k) => (k === i ? { ...p, ...patch } : p)))
  const remove = (i: number) => setEdited(prev => prev.filter((_, k) => k !== i))
  const move = (i: number, dir: -1 | 1) =>
    setEdited(prev => {
      const j = i + dir
      if (j < 0 || j >= prev.length) return prev
      const next = [...prev]
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })
  const add = () =>
    setEdited(prev => [...prev, { sub: '', feature: '', scope: '', priority: 'P2' }])

  const confirm = () => {
    if (hasEmptyFeature || valid.length === 0) return
    onConfirm(valid.map(p => ({ ...p, sub: tidySub(p.sub), feature: p.feature.trim(), scope: p.scope.trim() })))
  }

  if (loading) {
    return (
      <div className="bg-emerald-50/70 border border-emerald-200 rounded-xl p-4 space-y-2">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm text-emerald-900">
            <Loader2 size={15} className="animate-spin text-emerald-600" />
            <span>正在通读需求识别功能点（模块「{moduleName}」）… 约 1 分钟，识别完成后可先确认修改再生成用例。</span>
          </div>
          {onCancel && (
            <button
              type="button"
              onClick={onCancel}
              className="inline-flex items-center gap-1 px-2.5 py-1 text-xs text-gray-600 border border-gray-300 rounded-full hover:bg-gray-50 whitespace-nowrap"
            >
              <Square size={11} /> 停止
            </button>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="bg-emerald-50/70 border border-emerald-200 rounded-xl p-4 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-emerald-800">
          <ListChecks size={16} />
          确认功能点清单
        </div>
        <div className="text-xs text-emerald-700/80">
          模块「{moduleName}」 · 编号前缀 {casePrefix}
        </div>
      </div>

      <div className="text-xs text-emerald-700/80 leading-relaxed">
        大模型已从需求中识别出 <b>{points.length}</b> 个功能点，之后会<b>按功能点</b>分批编写用例（每个功能点约 3~6 条）。
        删掉不需要测的、补上遗漏的、改「覆盖范围」里的判定点——生成的用例数量和内容由这份清单决定。
        编号段只能用大写字母、数字、短横线，留空则自动编号。
      </div>

      <ul className="space-y-2 max-h-[28rem] overflow-auto pr-1">
        {edited.map((p, i) => (
          <li key={i} className="bg-white border border-emerald-200/80 rounded-lg p-2.5 space-y-1.5">
            <div className="flex items-center gap-2">
              <span className="w-5 text-[11px] text-gray-400 text-right flex-shrink-0">{i + 1}</span>
              <input
                value={p.feature}
                onChange={e => update(i, { feature: e.target.value })}
                disabled={busy}
                placeholder="功能点名称（必填）"
                className={`flex-1 min-w-0 px-2 py-1 text-sm border rounded focus:outline-none focus:ring-1 focus:ring-emerald-400 disabled:opacity-50 ${
                  p.feature.trim() ? 'border-gray-200' : 'border-red-300 bg-red-50/40'
                }`}
              />
              <select
                value={p.priority}
                onChange={e => update(i, { priority: e.target.value as TestPoint['priority'] })}
                disabled={busy}
                className="px-1.5 py-1 text-xs border border-gray-200 rounded bg-white focus:outline-none focus:ring-1 focus:ring-emerald-400 disabled:opacity-50"
              >
                {PRIORITIES.map(x => <option key={x} value={x}>{x}</option>)}
              </select>
              <input
                value={p.sub}
                onChange={e => update(i, { sub: e.target.value })}
                onBlur={e => update(i, { sub: tidySub(e.target.value) })}
                disabled={busy}
                placeholder="编号段"
                title={`用例编号：${casePrefix}-${p.sub || '…'}-001`}
                className="w-36 px-2 py-1 text-xs font-mono border border-gray-200 rounded focus:outline-none focus:ring-1 focus:ring-emerald-400 disabled:opacity-50"
              />
              <div className="flex items-center gap-0.5 flex-shrink-0">
                <button type="button" onClick={() => move(i, -1)} disabled={busy || i === 0} title="上移"
                  className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-gray-100 disabled:opacity-30">
                  <ArrowUp size={13} />
                </button>
                <button type="button" onClick={() => move(i, 1)} disabled={busy || i === edited.length - 1} title="下移"
                  className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-gray-100 disabled:opacity-30">
                  <ArrowDown size={13} />
                </button>
                <button type="button" onClick={() => remove(i)} disabled={busy} title="删除该功能点"
                  className="p-1 rounded text-gray-400 hover:text-red-600 hover:bg-red-50 disabled:opacity-30">
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
            <textarea
              value={p.scope}
              onChange={e => update(i, { scope: e.target.value })}
              disabled={busy}
              rows={2}
              placeholder="覆盖范围 / 关键判定点：写用例时会逐条对照，这里没提到的判定点就不会有用例"
              className="w-full ml-7 max-w-[calc(100%-1.75rem)] px-2 py-1 text-xs text-gray-700 border border-gray-200 rounded resize-y focus:outline-none focus:ring-1 focus:ring-emerald-400 disabled:opacity-50"
            />
          </li>
        ))}
      </ul>

      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={add}
            disabled={busy}
            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs text-emerald-800 border border-emerald-300 rounded-full hover:bg-emerald-100 disabled:opacity-50"
          >
            <Plus size={12} /> 新增功能点
          </button>
          <button
            type="button"
            onClick={onReextract}
            disabled={busy}
            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs text-gray-600 border border-gray-300 rounded-full hover:bg-gray-50 disabled:opacity-50"
          >
            <RefreshCw size={12} /> 重新识别
          </button>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-xs text-emerald-700/80">
            {valid.length} 个功能点 · 预计分 {batches} 批
            {hasEmptyFeature && <span className="text-red-600 ml-2">有功能点名称为空</span>}
          </span>
          <button
            type="button"
            onClick={confirm}
            disabled={busy || hasEmptyFeature || valid.length === 0}
            className="inline-flex items-center gap-1.5 px-4 py-1.5 text-xs font-medium text-white bg-emerald-600 rounded-full hover:bg-emerald-700 disabled:opacity-50 whitespace-nowrap"
          >
            {submitting
              ? <><Loader2 size={13} className="animate-spin" /> 生成中…</>
              : <><Sparkles size={13} /> 按这 {valid.length} 个功能点生成用例</>}
          </button>
        </div>
      </div>
    </div>
  )
}
