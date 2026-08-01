import { useEffect, useMemo, useState } from 'react'
import { Loader2, BookOpen, Sparkles, SearchX } from 'lucide-react'
import type { KnowledgeHit, KnowledgeNearMiss } from '../api/client'

interface Props {
  loading: boolean
  hits: KnowledgeHit[]
  // 未命中时「差一点入选」的候选（只读展示，不可勾选注入）
  nearMisses?: KnowledgeNearMiss[]
  onConfirm: (selectedIds: number[]) => void
  // 显示文案上的小提示（当前轮 N、模块、前缀），仅 UI 装饰
  moduleName?: string | null
  casePrefix?: string | null
  // 'clarify' = 注入到澄清提示词；'generate' = 注入到生成提示词。仅影响标题与按钮文案。
  phase?: 'clarify' | 'generate'
}

// 知识类型 → 中文标签
const TYPE_LABEL: Record<string, string> = {
  product_rule: '产品规则',
  module_relation: '模块关系',
  defect_pattern: '缺陷模式',
  term: '术语',
  constraint: '约束',
}

function formatDistance(d: number | null): string {
  if (d == null) return '—'
  // 距离越小越相关；翻成"相关度 0~100%" 让普通用户看得懂
  // 经验阈值：cosine distance 0.0~1.0 之间，0.2 以下相当相关
  const score = Math.max(0, Math.min(1, 1 - d))
  return `${Math.round(score * 100)}%`
}

export default function KnowledgePreviewPanel({
  loading, hits, nearMisses, onConfirm, moduleName, casePrefix, phase = 'generate',
}: Props) {
  // 默认全勾。每次 hits 变化（重新拉取）都重置一次。
  const [selected, setSelected] = useState<Set<number>>(new Set())

  // hits 是父组件异步拉取的结果，到达时需要重置勾选；属于"按 props 派生 state"的合理用法。
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSelected(new Set(hits.map(h => h.id)))
  }, [hits])

  const allChecked = hits.length > 0 && selected.size === hits.length
  const noneChecked = selected.size === 0

  const toggle = (id: number) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleAll = () => {
    setSelected(allChecked ? new Set() : new Set(hits.map(h => h.id)))
  }

  const sortedHits = useMemo(() => {
    // 距离从小到大；fallback 路径 distance=null 排到末尾
    return [...hits].sort((a, b) => {
      if (a.distance == null && b.distance == null) return 0
      if (a.distance == null) return 1
      if (b.distance == null) return -1
      return a.distance - b.distance
    })
  }, [hits])

  const sortedMisses = useMemo(() => {
    const list = nearMisses ?? []
    return [...list].sort((a, b) => {
      if (a.distance == null && b.distance == null) return 0
      if (a.distance == null) return 1
      if (b.distance == null) return -1
      return a.distance - b.distance
    })
  }, [nearMisses])

  const isClarify = phase === 'clarify'
  const headlineText = isClarify
    ? '确认要注入到澄清提示词的知识库内容'
    : '确认要注入到生成 prompt 的知识库内容'
  const emptyText = isClarify
    ? '知识库未命中相关条目，将直接基于本文档启动澄清。'
    : '知识库未命中相关条目，将直接基于本文档与澄清答案生成测试用例。'
  const confirmText = noneChecked
    ? (isClarify ? '不注入知识，开始澄清' : '不注入知识，直接生成')
    : (isClarify ? '使用所选知识开始澄清' : '使用所选知识开始生成')

  return (
    <div className="bg-emerald-50/70 border border-emerald-200 rounded-xl p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold text-emerald-800">
          <BookOpen size={16} />
          {headlineText}
        </div>
        {(moduleName || casePrefix) && (
          <div className="text-xs text-emerald-700/80">
            模块「{moduleName}」 · 前缀 {casePrefix}
          </div>
        )}
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-emerald-700">
          <Loader2 size={14} className="animate-spin" />
          正在检索项目知识库…
        </div>
      ) : hits.length === 0 ? (
        <div className="space-y-2">
          <div className="text-xs text-emerald-700/80">
            {emptyText}
          </div>
          {sortedMisses.length > 0 && (
            <div className="rounded-lg border border-emerald-200/70 bg-white/60 p-2.5 space-y-1.5">
              <div className="flex items-center gap-1.5 text-xs font-medium text-gray-600">
                <SearchX size={13} />
                最接近但未采用的 {sortedMisses.length} 条（仅供参考，不会注入）
              </div>
              <ul className="space-y-1.5 max-h-60 overflow-auto pr-1">
                {sortedMisses.map(m => {
                  const typeLabel = TYPE_LABEL[m.knowledge_type] || m.knowledge_type
                  return (
                    <li
                      key={m.id}
                      className="px-2.5 py-1.5 rounded border border-gray-200 bg-gray-50/70 text-xs leading-relaxed"
                    >
                      <div className="flex items-center flex-wrap gap-1.5 mb-0.5">
                        <span className="px-1.5 py-0.5 rounded bg-gray-200 text-gray-600 font-medium">
                          {typeLabel}
                        </span>
                        <span className="text-gray-500">
                          相关度 {formatDistance(m.distance)}
                        </span>
                        {m.reason && (
                          <span className="px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">
                            {m.reason}
                          </span>
                        )}
                      </div>
                      <div className="text-gray-700 whitespace-pre-wrap break-words">
                        {m.content}
                      </div>
                    </li>
                  )
                })}
              </ul>
            </div>
          )}
        </div>
      ) : (
        <>
          <div className="flex items-center justify-between text-xs text-emerald-700/80">
            <span>
              检索到 <b>{hits.length}</b> 条候选条目，已默认全部勾选；可取消不需要的。
            </span>
            <button
              type="button"
              onClick={toggleAll}
              className="px-2 py-0.5 rounded border border-emerald-300 hover:bg-emerald-100 transition"
            >
              {allChecked ? '全部取消' : '全部勾选'}
            </button>
          </div>

          <ul className="space-y-1.5 max-h-72 overflow-auto pr-1">
            {sortedHits.map(h => {
              const checked = selected.has(h.id)
              const typeLabel = TYPE_LABEL[h.knowledge_type] || h.knowledge_type
              return (
                <li
                  key={h.id}
                  className={`flex items-start gap-2 px-2.5 py-1.5 rounded border text-xs leading-relaxed transition cursor-pointer ${
                    checked
                      ? 'bg-white border-emerald-300'
                      : 'bg-emerald-50/30 border-emerald-100 opacity-60'
                  }`}
                  onClick={() => toggle(h.id)}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggle(h.id)}
                    onClick={e => e.stopPropagation()}
                    className="mt-0.5 accent-emerald-600"
                  />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5 mb-0.5">
                      <span className="px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-700 font-medium">
                        {typeLabel}
                      </span>
                      <span className="text-emerald-700/70">
                        相关度 {formatDistance(h.distance)}
                      </span>
                      {h.confidence > 0 && (
                        <span className="text-gray-400">
                          · 置信 {(h.confidence * 100).toFixed(0)}%
                        </span>
                      )}
                    </div>
                    <div className="text-gray-800 whitespace-pre-wrap break-words">
                      {h.content}
                    </div>
                  </div>
                </li>
              )
            })}
          </ul>
        </>
      )}

      <div className="flex items-center justify-end gap-2 pt-1">
        {!loading && hits.length > 0 && (
          <span className="text-xs text-emerald-700/80 mr-auto">
            将注入 <b>{selected.size}</b> / {hits.length} 条
          </span>
        )}
        <button
          type="button"
          disabled={loading}
          onClick={() => onConfirm(Array.from(selected))}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed transition"
        >
          <Sparkles size={14} />
          {confirmText}
        </button>
      </div>
    </div>
  )
}
