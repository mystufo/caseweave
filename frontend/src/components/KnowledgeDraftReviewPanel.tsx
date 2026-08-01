import { useEffect, useState } from 'react'
import { Sparkles, Trash2, BookPlus, Edit2, Check, AlertTriangle, GitCompare } from 'lucide-react'
import type { KnowledgeDraft, ModuleSummary } from '../api/client'

interface Props {
  // 文档信息——决定调哪一份 documents/{id}/confirm_pending_knowledge
  documentId: number
  role?: 'prd' | 'mindmap'
  // 需求文档与脑图合并抽取时为 true —— 标题体现"PRD + 脑图（冲突以脑图为准）"
  combined?: boolean
  filename?: string | null
  moduleName?: string | null
  // "加入哪个模块"下拉的数据源 + 默认选中（通常为上一步确认的模块 id；null=不归入模块）
  modules?: ModuleSummary[]
  defaultModuleId?: number | null
  drafts: KnowledgeDraft[]
  // submitting=true 时 disabled 所有按钮，防重复点击
  submitting?: boolean
  // 入库（按勾选 + 决策 + 已编辑后的草稿）/ 全部丢弃。父组件负责调后端接口并 settle 草稿状态。
  // moduleChoice 承载用户在下拉里选的入库模块（applyModule=true 时后端按它入库并回写文档归属）。
  onConfirm: (
    acceptedDrafts: KnowledgeDraft[],
    moduleChoice: { applyModule: boolean; moduleId: number | null },
  ) => void
  onDiscard: () => void
}

const TYPE_LABEL: Record<string, string> = {
  product_rule: '产品规则',
  module_relation: '模块关系',
  defect_pattern: '缺陷模式',
  term: '术语',
  constraint: '约束',
  ui_behavior: 'UI 行为',
}

const TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: 'product_rule', label: '产品规则' },
  { value: 'module_relation', label: '模块关系' },
  { value: 'defect_pattern', label: '缺陷模式' },
  { value: 'term', label: '术语' },
  { value: 'constraint', label: '约束' },
  { value: 'ui_behavior', label: 'UI 行为' },
]

// 相似/冲突草稿的三选一决策
type Resolution = 'keep_new' | 'keep_old' | 'keep_both'

// 一条草稿是否需要"保留哪个"决策：判定为 similar/conflict 且确有近邻。
// 纯函数（只看草稿本身），便于在 effect 内直接调用而不产生依赖闭包。
const draftNeedsDecision = (d: KnowledgeDraft | undefined) => {
  const s = d?.relation_status
  return (s === 'similar' || s === 'conflict') && (d?.conflicts?.length ?? 0) > 0
}

export default function KnowledgeDraftReviewPanel({
  documentId, role, combined, filename, moduleName, modules, defaultModuleId, drafts, submitting, onConfirm, onDiscard,
}: Props) {
  const needsDecision = (i: number) => draftNeedsDecision(drafts[i])
  // 普通（无冲突）草稿的索引——只有这些走 checkbox 勾选逻辑。
  const plainIndices = drafts.map((_, i) => i).filter(i => !needsDecision(i))

  // 普通草稿默认全勾。drafts 变化（切会话 / 重新抽取）时重置一次。
  const [selected, setSelected] = useState<Set<number>>(() => new Set(plainIndices))
  // 相似/冲突草稿的决策：按 (草稿索引 → 旧条目 id → 决策)，每条命中的已有记录各自独立三选，
  // 默认"两个都保留"（最保守：不删旧、不弃新）。
  const [resolutions, setResolutions] = useState<Record<number, Record<number, Resolution>>>(() => {
    const init: Record<number, Record<number, Resolution>> = {}
    drafts.forEach((d, i) => {
      if (!draftNeedsDecision(d)) return
      const per: Record<number, Resolution> = {}
      ;(d.conflicts || []).forEach(c => { per[c.entry_id] = 'keep_both' })
      init[i] = per
    })
    return init
  })
  // 用户编辑后的草稿副本——和 props.drafts 等长，索引一一对应。
  const [edited, setEdited] = useState<KnowledgeDraft[]>(() => drafts.map(d => ({ ...d })))
  // 当前哪条正在编辑模式（一次只编辑一条，避免 UI 太重）
  const [editingIdx, setEditingIdx] = useState<number | null>(null)
  // "加入哪个模块"选中值——默认取上一步确认的模块 id（null=不归入模块）。
  const [targetModuleId, setTargetModuleId] = useState<number | null>(defaultModuleId ?? null)

  // drafts 由父组件异步送达，到达/变化时重算默认勾选与冲突处置；按 props 派生 state。
  useEffect(() => {
    const plain = drafts.map((_, i) => i).filter(i => !draftNeedsDecision(drafts[i]))
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSelected(new Set(plain))
    const init: Record<number, Record<number, Resolution>> = {}
    drafts.forEach((d, i) => {
      if (!draftNeedsDecision(d)) return
      const per: Record<number, Resolution> = {}
      ;(d.conflicts || []).forEach(c => { per[c.entry_id] = 'keep_both' })
      init[i] = per
    })
    setResolutions(init)
    setEdited(drafts.map(d => ({ ...d })))
    setEditingIdx(null)
  }, [drafts])

  // 某条草稿最终是否会入库：决策草稿看每条命中记录的决策——只要有任一记录选了
  // "保留新的"或"两个都保留"就入库（仅当所有命中记录都选"保留旧的"才整体弃稿）；
  // 普通草稿看勾选。
  const willStore = (i: number) => {
    if (!needsDecision(i)) return selected.has(i)
    const per = resolutions[i] || {}
    const decisions = (drafts[i]?.conflicts || []).map(c => per[c.entry_id] || 'keep_both')
    return decisions.some(r => r !== 'keep_old')
  }

  const storeCount = drafts.reduce((n, _, i) => (willStore(i) ? n + 1 : n), 0)

  const allPlainChecked = plainIndices.length > 0 && plainIndices.every(i => selected.has(i))

  const toggle = (i: number) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })
  }

  const toggleAllPlain = () => {
    setSelected(allPlainChecked ? new Set() : new Set(plainIndices))
  }

  const setResolution = (i: number, entryId: number, r: Resolution) => {
    setResolutions(prev => ({ ...prev, [i]: { ...(prev[i] || {}), [entryId]: r } }))
  }

  const updateEdited = (i: number, patch: Partial<KnowledgeDraft>) => {
    setEdited(prev => prev.map((d, j) => (j === i ? { ...d, ...patch } : d)))
  }

  const handleConfirm = () => {
    const accepted: KnowledgeDraft[] = []
    edited.forEach((d, i) => {
      if (!willStore(i)) return
      if (!(d.content.trim().length > 0 && d.knowledge_type.trim().length > 0)) return
      // 逐条命中记录派生：仅把选了"保留新的"的旧条目 id 带上（后端入库同时删除它们）；
      // 选"保留旧的/两个都保留"的旧条目保留不动。
      const per = resolutions[i] || {}
      const supersedes = needsDecision(i)
        ? (drafts[i].conflicts || []).filter(c => per[c.entry_id] === 'keep_new').map(c => c.entry_id)
        : undefined
      accepted.push({
        ...d,
        supersedes_entry_ids: supersedes && supersedes.length > 0 ? supersedes : undefined,
      })
    })
    // 有模块下拉时才带 applyModule；否则维持后端"沿用文档当前模块"的旧行为。
    onConfirm(accepted, { applyModule: !!modules, moduleId: targetModuleId })
  }

  const roleLabel = role === 'mindmap' ? '脑图' : 'PRD'
  const headline = combined
    ? '请审核从需求文档 + 脑图合并抽取出的产品知识草稿（冲突以脑图为准）'
    : `请审核从${roleLabel}抽取出的产品知识草稿`

  const decisionCount = drafts.filter((_, i) => needsDecision(i)).length

  return (
    <div className="bg-amber-50/70 border border-amber-200 rounded-xl p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold text-amber-800">
          <BookPlus size={16} />
          {headline}
        </div>
        {(filename || moduleName) && (
          <div className="text-xs text-amber-700/80">
            {filename ? <span>来源「{filename}」</span> : null}
            {moduleName ? <span className="ml-2">模块「{moduleName}」</span> : null}
          </div>
        )}
      </div>

      <div className="text-xs text-amber-700/80">
        勾选要永久写入项目知识库的条目；点 <Edit2 size={11} className="inline -mt-0.5" /> 可修订内容、改类型、调置信度。
        完全重复的条目已自动过滤；与库内已有内容<b>相似</b>或<b>冲突</b>的条目需你<b>逐条</b>选择保留哪一个。
      </div>

      {decisionCount > 0 && (
        <div className="flex items-start gap-2 text-xs text-orange-800 bg-orange-50 border border-orange-200 rounded-md px-3 py-2">
          <GitCompare size={13} className="mt-0.5 flex-shrink-0" />
          <span>
            有 <b>{decisionCount}</b> 条草稿与知识库已有内容相似或冲突，已为每条列出对照与判定理由，
            请<b>为每条命中的已有条目</b>选择「保留新的（替换此旧条目）／保留此旧条目／两个都保留」，默认「两个都保留」；
            只要有一条命中记录选了「保留新的」或「两个都保留」，该草稿就会入库。
          </span>
        </div>
      )}

      {/* 加入哪个模块——默认取上一步确认的模块；可改选其它模块或"不归入模块"（项目级）。 */}
      {modules && (
        <label className="flex items-center gap-2 text-xs text-amber-800">
          <span className="whitespace-nowrap">加入模块</span>
          <select
            value={targetModuleId != null ? String(targetModuleId) : '__none__'}
            onChange={e => setTargetModuleId(e.target.value === '__none__' ? null : Number(e.target.value))}
            disabled={submitting}
            className="flex-1 px-2 py-1 text-xs border border-amber-300 rounded bg-white focus:outline-none focus:ring-1 focus:ring-amber-400 disabled:opacity-50"
          >
            {modules.map(m => (
              <option key={m.id} value={String(m.id)}>
                {m.name}{m.code ? `（${m.code}）` : ''}
              </option>
            ))}
            <option value="__none__">不归入任何模块（项目级）</option>
          </select>
        </label>
      )}

      {plainIndices.length > 0 && (
        <div className="flex items-center justify-between text-xs text-amber-700/80">
          <span>
            共 <b>{drafts.length}</b> 条候选（其中 {plainIndices.length} 条无冲突，默认勾选）。
          </span>
          <button
            type="button"
            onClick={toggleAllPlain}
            disabled={submitting}
            className="px-2 py-0.5 rounded border border-amber-300 hover:bg-amber-100 transition disabled:opacity-50"
          >
            {allPlainChecked ? '全部取消' : '全部勾选'}
          </button>
        </div>
      )}

      <ul className="space-y-1.5 max-h-96 overflow-auto pr-1">
        {edited.map((d, i) => {
          const isEditing = editingIdx === i
          const typeLabel = TYPE_LABEL[d.knowledge_type] || d.knowledge_type
          const decision = needsDecision(i)
          const status = drafts[i]?.relation_status
          const conflicts = drafts[i]?.conflicts || []
          const isConflict = status === 'conflict'
          const stored = willStore(i)
          const perRes = resolutions[i] || {}

          // 卡片配色：冲突>相似>普通；不入库时淡化。
          const borderCls = decision
            ? isConflict
              ? 'bg-white border-red-300 ring-1 ring-red-100'
              : 'bg-white border-orange-300 ring-1 ring-orange-100'
            : stored
              ? 'bg-white border-amber-300'
              : 'bg-amber-50/30 border-amber-100 opacity-60'

          return (
            <li
              key={`${documentId}-${i}`}
              className={`flex items-start gap-2 px-2.5 py-1.5 rounded border text-xs leading-relaxed transition ${borderCls} ${
                isEditing ? 'border-amber-400 ring-1 ring-amber-200' : ''
              }`}
            >
              {/* 普通草稿用勾选框；决策草稿不显示勾选框（由下方 radio 决定去留）。 */}
              {!decision && (
                <input
                  type="checkbox"
                  checked={selected.has(i)}
                  disabled={submitting}
                  onChange={() => toggle(i)}
                  className="mt-1 accent-amber-600"
                />
              )}
              {decision && (
                <span
                  className={`mt-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium flex-shrink-0 ${
                    isConflict ? 'bg-red-100 text-red-700' : 'bg-orange-100 text-orange-700'
                  }`}
                >
                  {isConflict ? '冲突' : '相似'}
                </span>
              )}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5 mb-1">
                  {isEditing ? (
                    <select
                      value={d.knowledge_type}
                      onChange={e => updateEdited(i, { knowledge_type: e.target.value })}
                      disabled={submitting}
                      className="px-1.5 py-0.5 text-xs rounded border border-amber-300 bg-white focus:outline-none focus:ring-1 focus:ring-amber-400"
                    >
                      {TYPE_OPTIONS.map(o => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                      {!TYPE_OPTIONS.some(o => o.value === d.knowledge_type) && (
                        <option value={d.knowledge_type}>{d.knowledge_type}</option>
                      )}
                    </select>
                  ) : (
                    <span className="px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 font-medium">
                      {typeLabel}
                    </span>
                  )}
                  <span className="text-gray-400">
                    置信 {(d.confidence * 100).toFixed(0)}%
                  </span>
                  {isEditing && (
                    <input
                      type="range"
                      min={0.1}
                      max={0.95}
                      step={0.05}
                      value={d.confidence}
                      onChange={e => updateEdited(i, { confidence: parseFloat(e.target.value) })}
                      disabled={submitting}
                      className="flex-1 max-w-[120px] accent-amber-600"
                    />
                  )}
                  <div className="ml-auto flex items-center gap-1">
                    {isEditing ? (
                      <button
                        type="button"
                        onClick={() => setEditingIdx(null)}
                        disabled={submitting}
                        title="完成编辑"
                        className="p-1 text-emerald-600 hover:bg-emerald-50 rounded disabled:opacity-50"
                      >
                        <Check size={12} />
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setEditingIdx(i)}
                        disabled={submitting}
                        title="编辑"
                        className="p-1 text-amber-700 hover:bg-amber-100 rounded disabled:opacity-50"
                      >
                        <Edit2 size={12} />
                      </button>
                    )}
                  </div>
                </div>
                {isEditing ? (
                  <textarea
                    value={d.content}
                    onChange={e => updateEdited(i, { content: e.target.value })}
                    disabled={submitting}
                    rows={Math.min(6, Math.max(2, d.content.split('\n').length + 1))}
                    className="w-full px-2 py-1.5 text-xs border border-amber-300 rounded focus:outline-none focus:ring-1 focus:ring-amber-400 bg-white"
                  />
                ) : (
                  <div
                    className={`text-gray-800 whitespace-pre-wrap break-words ${decision ? '' : 'cursor-pointer'}`}
                    onClick={() => !decision && !submitting && toggle(i)}
                  >
                    <span className="text-[10px] text-amber-600 mr-1">新草稿：</span>
                    {d.content}
                  </div>
                )}

                {/* 相似 / 冲突：逐条列出库内对照条目 + 判定理由 + 该条独立三选一决策 */}
                {decision && (
                  <div
                    className={`mt-1.5 rounded border px-2 py-1.5 ${
                      isConflict ? 'border-red-200 bg-red-50/70' : 'border-orange-200 bg-orange-50/70'
                    }`}
                  >
                    <div className={`flex items-center gap-1 text-[11px] font-medium mb-1 ${
                      isConflict ? 'text-red-700' : 'text-orange-700'
                    }`}>
                      <AlertTriangle size={11} />
                      <span>
                        与知识库已有 {conflicts.length} 条{isConflict ? '冲突' : '相似'}，请为每条选择保留策略
                      </span>
                    </div>
                    <ul className="space-y-1.5 pl-1">
                      {conflicts.map(c => {
                        const cRes = perRes[c.entry_id] || 'keep_both'
                        const cConflict = c.relation === 'conflict'
                        return (
                          <li
                            key={c.entry_id}
                            className={`text-[11px] border-l-2 pl-2 py-0.5 ${
                              cConflict
                                ? 'border-red-300 text-red-900/90'
                                : 'border-orange-300 text-orange-900/90'
                            }`}
                          >
                            <div className="flex items-center gap-1.5 mb-0.5">
                              <span className={`px-1 py-px rounded font-medium ${
                                cConflict ? 'bg-red-100 text-red-700' : 'bg-orange-100 text-orange-700'
                              }`}>
                                {cConflict ? '冲突' : '相似'}
                              </span>
                              <span className="text-[10px] text-gray-500">
                                {TYPE_LABEL[c.knowledge_type] || c.knowledge_type} · 相似度 {((1 - c.distance) * 100).toFixed(0)}%
                              </span>
                            </div>
                            <div className="whitespace-pre-wrap break-words">
                              <span className="text-[10px] text-gray-500 mr-1">已有：</span>{c.content}
                            </div>
                            {c.reason && (
                              <div className="text-[10px] text-gray-500 mt-0.5">判定理由：{c.reason}</div>
                            )}
                            {/* 该条命中记录的三选一 */}
                            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1">
                              {([
                                ['keep_new', '保留新的（替换此旧条目）'],
                                ['keep_old', '保留此旧条目'],
                                ['keep_both', '两个都保留'],
                              ] as [Resolution, string][]).map(([val, label]) => (
                                <label key={val} className="flex items-center gap-1 text-[11px] text-gray-700 cursor-pointer">
                                  <input
                                    type="radio"
                                    name={`res-${documentId}-${i}-${c.entry_id}`}
                                    checked={cRes === val}
                                    disabled={submitting}
                                    onChange={() => setResolution(i, c.entry_id, val)}
                                    className={cConflict ? 'accent-red-600' : 'accent-orange-600'}
                                  />
                                  {label}
                                </label>
                              ))}
                            </div>
                          </li>
                        )
                      })}
                    </ul>
                  </div>
                )}
              </div>
            </li>
          )
        })}
      </ul>

      <div className="flex items-center justify-end gap-2 pt-1">
        <span className="text-xs text-amber-700/80 mr-auto">
          将入库 <b>{storeCount}</b> / {drafts.length} 条
          {editingIdx != null && (
            <span className="ml-2 text-amber-600">（正在编辑第 {editingIdx + 1} 条，记得点 <Check size={11} className="inline -mt-0.5" /> 完成）</span>
          )}
        </span>
        <button
          type="button"
          disabled={submitting}
          onClick={onDiscard}
          className="inline-flex items-center gap-1 px-3 py-1.5 rounded-md border border-amber-300 text-amber-700 text-sm hover:bg-amber-100 disabled:opacity-50 transition"
        >
          <Trash2 size={14} />
          全部丢弃
        </button>
        <button
          type="button"
          disabled={submitting}
          onClick={handleConfirm}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-amber-600 text-white text-sm font-medium hover:bg-amber-700 disabled:opacity-50 disabled:cursor-not-allowed transition"
        >
          <Sparkles size={14} />
          {storeCount === 0 ? '不入库，继续' : `入库 ${storeCount} 条并继续`}
        </button>
      </div>
    </div>
  )
}
