import { useEffect, useState } from 'react'
import { Sparkles, Trash2, BookPlus, Edit2, Check, AlertTriangle, ChevronDown, ChevronUp } from 'lucide-react'
import type { KnowledgeDraft } from '../api/client'

interface Props {
  // 文档信息——决定调哪一份 documents/{id}/confirm_pending_knowledge
  documentId: number
  role?: 'prd' | 'mindmap'
  filename?: string | null
  moduleName?: string | null
  drafts: KnowledgeDraft[]
  // submitting=true 时 disabled 所有按钮，防重复点击
  submitting?: boolean
  // 入库（按勾选 + 已编辑后的草稿）/ 全部丢弃。父组件负责调后端接口并 settle 草稿状态。
  onConfirm: (acceptedDrafts: KnowledgeDraft[]) => void
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

export default function KnowledgeDraftReviewPanel({
  documentId, role, filename, moduleName, drafts, submitting, onConfirm, onDiscard,
}: Props) {
  // 默认全勾。drafts 变化（切会话 / 重新抽取）时重置一次。
  const [selected, setSelected] = useState<Set<number>>(() => new Set(drafts.map((_, i) => i)))
  // 用户编辑后的草稿副本——和 props.drafts 等长，索引一一对应。
  const [edited, setEdited] = useState<KnowledgeDraft[]>(() => drafts.map(d => ({ ...d })))
  // 当前哪条正在编辑模式（一次只编辑一条，避免 UI 太重）
  const [editingIdx, setEditingIdx] = useState<number | null>(null)
  // 哪些行展开了"潜在冲突"详情（按索引）。冲突默认折叠。
  const [expandedConflicts, setExpandedConflicts] = useState<Set<number>>(new Set())

  useEffect(() => {
    setSelected(new Set(drafts.map((_, i) => i)))
    setEdited(drafts.map(d => ({ ...d })))
    setEditingIdx(null)
    setExpandedConflicts(new Set())
  }, [drafts])

  const toggleConflicts = (i: number) => {
    setExpandedConflicts(prev => {
      const next = new Set(prev)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })
  }

  const allChecked = drafts.length > 0 && selected.size === drafts.length
  const noneChecked = selected.size === 0

  const toggle = (i: number) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })
  }

  const toggleAll = () => {
    setSelected(allChecked ? new Set() : new Set(drafts.map((_, i) => i)))
  }

  const updateEdited = (i: number, patch: Partial<KnowledgeDraft>) => {
    setEdited(prev => prev.map((d, j) => (j === i ? { ...d, ...patch } : d)))
  }

  const handleConfirm = () => {
    // 按勾选顺序输出——空 content 的条目自动剔除（保险）
    const indices = Array.from(selected).sort((a, b) => a - b)
    const accepted = indices
      .map(i => edited[i])
      .filter(d => d && d.content.trim().length > 0 && d.knowledge_type.trim().length > 0)
    onConfirm(accepted)
  }

  const roleLabel = role === 'mindmap' ? '脑图' : 'PRD'
  const headline = `请审核从${roleLabel}抽取出的产品知识草稿`

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
        草稿是基于本次上传文档抽取出的可沉淀产品知识，入库后将参与未来同模块用例生成的检索召回。
      </div>

      {drafts.length > 0 && (
        <div className="flex items-center justify-between text-xs text-amber-700/80">
          <span>
            共 <b>{drafts.length}</b> 条候选，默认全部勾选；可取消、修订或改类型。
          </span>
          <button
            type="button"
            onClick={toggleAll}
            disabled={submitting}
            className="px-2 py-0.5 rounded border border-amber-300 hover:bg-amber-100 transition disabled:opacity-50"
          >
            {allChecked ? '全部取消' : '全部勾选'}
          </button>
        </div>
      )}

      <ul className="space-y-1.5 max-h-96 overflow-auto pr-1">
        {edited.map((d, i) => {
          const checked = selected.has(i)
          const isEditing = editingIdx === i
          const typeLabel = TYPE_LABEL[d.knowledge_type] || d.knowledge_type
          const conflicts = d.conflicts || []
          const hasConflicts = conflicts.length > 0
          const showConflicts = expandedConflicts.has(i)
          return (
            <li
              key={`${documentId}-${i}`}
              className={`flex items-start gap-2 px-2.5 py-1.5 rounded border text-xs leading-relaxed transition ${
                checked
                  ? hasConflicts
                    ? 'bg-white border-orange-300 ring-1 ring-orange-100'
                    : 'bg-white border-amber-300'
                  : 'bg-amber-50/30 border-amber-100 opacity-60'
              } ${isEditing ? 'border-amber-400 ring-1 ring-amber-200' : ''}`}
            >
              <input
                type="checkbox"
                checked={checked}
                disabled={submitting}
                onChange={() => toggle(i)}
                className="mt-1 accent-amber-600"
              />
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
                    className="text-gray-800 whitespace-pre-wrap break-words cursor-pointer"
                    onClick={() => !submitting && toggle(i)}
                  >
                    {d.content}
                  </div>
                )}
                {hasConflicts && (
                  <div className="mt-1.5 rounded border border-orange-200 bg-orange-50/70 px-2 py-1">
                    <button
                      type="button"
                      onClick={() => toggleConflicts(i)}
                      className="flex items-center gap-1 text-[11px] font-medium text-orange-700 hover:text-orange-900 transition"
                    >
                      <AlertTriangle size={11} />
                      <span>可能与已有 {conflicts.length} 条知识冲突</span>
                      {showConflicts ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
                    </button>
                    {showConflicts && (
                      <ul className="mt-1.5 space-y-1 pl-1">
                        {conflicts.map(c => (
                          <li key={c.entry_id} className="text-[11px] text-orange-900/90 border-l-2 border-orange-300 pl-2 py-0.5">
                            <div className="flex items-center gap-1.5 mb-0.5 text-orange-700">
                              <span className="px-1 py-px rounded bg-orange-100 font-medium">
                                {TYPE_LABEL[c.knowledge_type] || c.knowledge_type}
                              </span>
                              <span className="text-[10px] text-orange-600/80">
                                相似度 {((1 - c.distance) * 100).toFixed(0)}% · 置信 {(c.confidence * 100).toFixed(0)}%
                              </span>
                            </div>
                            <div className="whitespace-pre-wrap break-words">{c.content}</div>
                          </li>
                        ))}
                        <li className="text-[10px] text-orange-700/70 pl-2 pt-0.5">
                          建议：若本草稿是更新版，请去"知识库"页删除上面的旧条目；若仅是补充则可直接入库。
                        </li>
                      </ul>
                    )}
                  </div>
                )}
              </div>
            </li>
          )
        })}
      </ul>

      <div className="flex items-center justify-end gap-2 pt-1">
        <span className="text-xs text-amber-700/80 mr-auto">
          将入库 <b>{selected.size}</b> / {drafts.length} 条
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
          {noneChecked ? '不入库，继续' : `入库 ${selected.size} 条并继续`}
        </button>
      </div>
    </div>
  )
}
