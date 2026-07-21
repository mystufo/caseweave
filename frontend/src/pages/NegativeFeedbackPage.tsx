import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchModules,
  fetchNegativeFeedback,
  fetchEvolutionSummary,
  type ModuleSummary,
  type NegativeFeedbackRecord,
  type EvolutionSummary,
} from '../api/client'
import TabBar, { type ViewKey } from '../components/TabBar'
import {
  ThumbsDown, RefreshCw, Loader2, Edit2, MessageSquareWarning,
  ChevronDown, ChevronRight, Sparkles, BookOpen, Brain, FileCog, LineChart,
} from 'lucide-react'

interface PageProps {
  view: ViewKey
  onChangeView: (v: ViewKey) => void
}

const ALL = -1  // 模块过滤「全部」哨兵

// diff_analyzer 归纳出的规则类型 → 中文
const RULE_TYPE_LABEL: Record<string, string> = {
  product_rule: '产品规则',
  constraint: '约束',
  ui_behavior: 'UI 行为',
}

// 被修改字段 → 中文（与 diff_analyzer.WATCHED_FIELDS 对应）
const FIELD_LABEL: Record<string, string> = {
  name: '用例名称',
  preconditions: '前置条件',
  steps: '操作步骤',
  expected_result: '预期结果',
  remarks: '备注',
  priority: '优先级',
}

// 分诊 / 消费出口 → 中文 + 图标
const OUTPUT_META: Record<string, { label: string; icon: typeof BookOpen }> = {
  knowledge: { label: '知识库', icon: BookOpen },
  skill: { label: 'Skill', icon: Brain },
  prompt: { label: '系统提示词', icon: FileCog },
}

type TypeFilter = 'all' | 'edit' | 'dislike'

function formatDate(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

/** 单条负反馈归纳记录卡片。 */
function FeedbackCard({ record }: { record: NegativeFeedbackRecord }) {
  const [expanded, setExpanded] = useState(false)
  const isEdit = record.feedback_type === 'edit'
  const hasRules = record.extracted_rules.length > 0
  const hasDetail = hasRules || !!record.reason || record.changed_fields.length > 0

  return (
    <div className="bg-white border border-gray-200 rounded-lg px-4 py-3 hover:border-rose-200 transition">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          {/* 头部标签行 */}
          <div className="flex flex-wrap items-center gap-2 text-xs mb-1.5">
            <span
              className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border font-medium ${
                isEdit
                  ? 'bg-amber-100 text-amber-700 border-amber-200'
                  : 'bg-rose-100 text-rose-700 border-rose-200'
              }`}
            >
              {isEdit ? <Edit2 size={11} /> : <ThumbsDown size={11} />}
              {isEdit ? '人工修改' : '点踩'}
            </span>
            {record.intent && (
              <span className="px-1.5 py-0.5 rounded border bg-indigo-100 text-indigo-700 border-indigo-200">
                {record.intent}
              </span>
            )}
            <span className="text-gray-500">模块：{record.module || '未指定'}</span>
            <span className="text-gray-400">{formatDate(record.created_at)}</span>
          </div>

          {/* 关联用例 */}
          <div className="text-sm text-gray-800 mb-1">
            <span className="text-gray-400 mr-1">关联用例：</span>
            {record.test_case_name}
          </div>

          {/* 摘要 */}
          {record.summary && (
            <div className="text-sm text-gray-700 leading-relaxed">
              {record.summary}
            </div>
          )}

          {/* 分诊 / 消费出口 */}
          {record.triage_targets.length > 0 && (
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] text-gray-400">流向出口：</span>
              {record.triage_targets.map(t => {
                const meta = OUTPUT_META[t]
                const consumed = record.consumed_by.includes(t)
                const Icon = meta?.icon || Sparkles
                return (
                  <span
                    key={t}
                    className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-[11px] ${
                      consumed
                        ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                        : 'bg-gray-50 text-gray-500 border-gray-200'
                    }`}
                    title={consumed ? '已被该出口消费' : '待该出口消费'}
                  >
                    <Icon size={10} />
                    {meta?.label || t}
                    <span className={consumed ? 'text-emerald-500' : 'text-amber-500'}>
                      {consumed ? '已消化' : '待消化'}
                    </span>
                  </span>
                )
              })}
            </div>
          )}

          {/* 展开区：改动字段 / 点踩原因 / 归纳规则全文 */}
          {hasDetail && (
            <>
              <button
                type="button"
                onClick={() => setExpanded(v => !v)}
                className="mt-2 inline-flex items-center gap-1 text-[11px] text-rose-600 hover:text-rose-800"
              >
                {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                {expanded ? '收起归纳详情' : '展开归纳详情'}
                {hasRules && (
                  <span className="ml-1 text-emerald-600">（沉淀 {record.extracted_rules.length} 条规则）</span>
                )}
              </button>

              {expanded && (
                <div className="mt-2 space-y-2.5 border-l-2 border-rose-100 pl-3">
                  {/* 改动字段 */}
                  {record.changed_fields.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-[11px] text-gray-400">改动字段：</span>
                      {record.changed_fields.map(f => (
                        <span
                          key={f}
                          className="px-1.5 py-0.5 text-[11px] rounded bg-gray-100 text-gray-600 border border-gray-200"
                        >
                          {FIELD_LABEL[f] || f}
                        </span>
                      ))}
                    </div>
                  )}

                  {/* 点踩原因 */}
                  {record.reason && (
                    <div className="text-xs">
                      <div className="text-[11px] text-gray-400 mb-0.5">点踩原因</div>
                      <div className="text-gray-700 bg-rose-50/60 border border-rose-100 rounded px-2.5 py-1.5 whitespace-pre-wrap break-words">
                        {record.reason}
                      </div>
                    </div>
                  )}

                  {/* 归纳出的规则全文 */}
                  {hasRules && (
                    <div className="space-y-1.5">
                      <div className="text-[11px] text-gray-400">本次归纳沉淀的规则</div>
                      {record.extracted_rules.map((r, i) => (
                        <div
                          key={i}
                          className="bg-emerald-50/50 border border-emerald-100 rounded px-2.5 py-1.5"
                        >
                          <div className="flex flex-wrap items-center gap-2 mb-1 text-[11px]">
                            <span className="px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-700 border border-emerald-200">
                              {RULE_TYPE_LABEL[r.knowledge_type || ''] || r.knowledge_type || '规则'}
                            </span>
                            {r.confidence != null && (
                              <span className="text-gray-400">
                                置信度 {(r.confidence * 100).toFixed(0)}%
                              </span>
                            )}
                          </div>
                          <div className="text-sm text-gray-800 whitespace-pre-wrap break-words leading-relaxed">
                            {r.content}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

export default function NegativeFeedbackPage({ view, onChangeView }: PageProps) {
  const [modules, setModules] = useState<ModuleSummary[]>([])
  const [activeModule, setActiveModule] = useState<number>(ALL)
  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all')
  const [items, setItems] = useState<NegativeFeedbackRecord[]>([])
  const [loading, setLoading] = useState(false)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  // 反馈进化总览（从知识库页迁移过来）：三出口 待消费/已消费 + intent 分布
  const [evolution, setEvolution] = useState<EvolutionSummary | null>(null)
  const reloadEvolution = useCallback(async () => {
    try {
      setEvolution(await fetchEvolutionSummary())
    } catch (err) {
      console.error('Evolution summary failed:', err)
    }
  }, [])

  const reload = useCallback(async () => {
    setLoading(true)
    setErrorMsg(null)
    try {
      const data = await fetchNegativeFeedback({
        moduleId: activeModule === ALL ? undefined : activeModule,
        feedbackType: typeFilter === 'all' ? undefined : typeFilter,
        limit: 100,
      })
      setItems(data)
    } catch (e) {
      console.error('Negative feedback load failed:', e)
      setErrorMsg('加载失败，请稍后重试')
      setItems([])
    } finally {
      setLoading(false)
    }
  }, [activeModule, typeFilter])

  useEffect(() => {
    fetchModules().then(setModules).catch(err => console.error('Modules load:', err))
    void reloadEvolution()
  }, [reloadEvolution])

  useEffect(() => {
    void reload()
  }, [reload])

  // 与 KnowledgePage 一致：从别的 tab 切入本页这一刻主动刷新（各页面始终挂载）。
  const refreshOnActivateRef = useRef<() => void>(() => {})
  refreshOnActivateRef.current = () => {
    void reload()
    void reloadEvolution()
    fetchModules().then(setModules).catch(err => console.error('Modules load:', err))
  }
  const prevViewRef = useRef<ViewKey>(view)
  useEffect(() => {
    if (view === 'feedback' && prevViewRef.current !== 'feedback') {
      refreshOnActivateRef.current()
    }
    prevViewRef.current = view
  }, [view])

  const counts = useMemo(() => {
    let edit = 0
    let dislike = 0
    for (const it of items) {
      if (it.feedback_type === 'edit') edit++
      else dislike++
    }
    return { edit, dislike, total: items.length }
  }, [items])

  const TYPE_TABS: { key: TypeFilter; label: string }[] = [
    { key: 'all', label: '全部' },
    { key: 'edit', label: '人工修改' },
    { key: 'dislike', label: '点踩' },
  ]

  return (
    <div className="flex h-full bg-gray-50 overflow-hidden">
      <TabBar value={view} onChange={onChangeView} />

      {/* 左侧：模块筛选 */}
      <aside className="w-56 bg-white border-r border-gray-200 flex-shrink-0 flex flex-col">
        <div className="px-4 py-3 border-b border-gray-200">
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide">按模块筛选</div>
        </div>
        <div className="flex-1 overflow-y-auto py-1">
          {[
            { id: ALL, name: '全部模块', desc: null as string | null },
            ...modules.map(m => ({ id: m.id, name: m.name, desc: m.description })),
          ].map(m => {
            const active = activeModule === m.id
            return (
              <button
                key={m.id}
                type="button"
                onClick={() => setActiveModule(m.id)}
                className={`w-full text-left px-4 py-2 text-sm transition-colors ${
                  active
                    ? 'bg-rose-50 text-rose-800 border-l-2 border-rose-500'
                    : 'text-gray-700 hover:bg-gray-50 border-l-2 border-transparent'
                }`}
                title={m.desc || undefined}
              >
                <div className="truncate font-medium">{m.name}</div>
                {m.desc && <div className="text-xs text-gray-400 truncate">{m.desc}</div>}
              </button>
            )
          })}
        </div>
      </aside>

      {/* 主区 */}
      <main className="flex-1 flex flex-col overflow-hidden">
        <header className="flex items-center gap-3 px-6 py-3 border-b border-gray-200 bg-white">
          <LineChart size={16} className="text-rose-600" />
          <h1 className="text-base font-semibold text-gray-800">进化报告</h1>
          <span className="text-xs text-gray-400">负反馈归纳记录与三出口进化去向</span>
          <div className="flex-1" />
          <button
            type="button"
            onClick={() => { void reload(); void reloadEvolution() }}
            className="p-1.5 text-gray-500 hover:text-gray-800 border border-gray-200 rounded-md"
            title="刷新"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          </button>
        </header>

        {/* 反馈进化总览（从知识库页迁移）：三出口 待消费/已消费 + 意图分布 */}
        {evolution && evolution.triaged_total > 0 && (
          <div className="px-6 py-3 border-b border-gray-200 bg-gradient-to-r from-indigo-50/60 to-violet-50/40">
            <div className="flex items-start gap-4 flex-wrap">
              <div className="flex items-center gap-2 text-xs font-semibold text-indigo-800/90">
                <Sparkles size={14} />
                反馈进化
                <span className="font-normal text-[11px] text-indigo-400">
                  负反馈已分诊 {evolution.triaged_total} 条，流向三个进化出口
                </span>
              </div>
              <div className="flex flex-wrap gap-3">
                {(['knowledge', 'skill', 'prompt'] as const).map(kind => {
                  const o = evolution.outputs[kind]
                  const label = OUTPUT_META[kind]?.label || kind
                  return (
                    <div
                      key={kind}
                      className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-white border border-indigo-100 text-xs"
                      title={`分诊到「${label}」的负反馈：${o.pending} 条待消费 / ${o.consumed} 条已消费`}
                    >
                      <span className="font-medium text-gray-700">{label}</span>
                      <span className="text-amber-600">待消化 {o.pending}</span>
                      <span className="text-gray-300">·</span>
                      <span className="text-emerald-600">已消化 {o.consumed}</span>
                    </div>
                  )
                })}
              </div>
              <div className="flex-1" />
              <button
                type="button"
                onClick={() => void reloadEvolution()}
                className="p-1 text-gray-400 hover:text-gray-700"
                title="刷新反馈进化总览"
              >
                <RefreshCw size={12} />
              </button>
            </div>
            {Object.keys(evolution.intent_distribution).length > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <span className="text-[11px] text-indigo-800/70">意图分布：</span>
                {Object.entries(evolution.intent_distribution)
                  .sort((a, b) => b[1] - a[1])
                  .map(([intent, count]) => (
                    <span
                      key={intent}
                      className="px-1.5 py-0.5 text-[11px] rounded border bg-white text-indigo-800/80 border-indigo-200"
                    >
                      {intent} · {count}
                    </span>
                  ))}
              </div>
            )}
          </div>
        )}

        {/* 类型过滤 + 计数 */}
        <div className="px-6 py-3 border-b border-gray-200 bg-gradient-to-r from-rose-50/60 to-orange-50/40">
          <div className="flex items-center gap-3 flex-wrap">
            <div className="flex items-center gap-1.5">
              {TYPE_TABS.map(t => {
                const active = typeFilter === t.key
                const n = t.key === 'all' ? counts.total : t.key === 'edit' ? counts.edit : counts.dislike
                return (
                  <button
                    key={t.key}
                    type="button"
                    onClick={() => setTypeFilter(t.key)}
                    className={`px-2.5 py-1 text-xs rounded-md border transition ${
                      active
                        ? 'bg-rose-600 text-white border-rose-600'
                        : 'bg-white text-gray-600 border-gray-200 hover:bg-rose-50'
                    }`}
                  >
                    {t.label} · {n}
                  </button>
                )
              })}
            </div>
            <div className="flex-1" />
            <div className="text-[11px] text-gray-400">
              仅展示已完成 AI 归纳的负反馈（编辑修改 + 带原因的点踩）
            </div>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-2">
          {errorMsg && (
            <div className="px-3 py-2 rounded bg-red-50 border border-red-200 text-sm text-red-700">
              {errorMsg}
            </div>
          )}

          {loading && items.length === 0 && (
            <div className="flex items-center justify-center py-16 text-sm text-gray-400">
              <Loader2 size={14} className="animate-spin mr-2" />
              加载中…
            </div>
          )}

          {!loading && items.length === 0 && !errorMsg && (
            <div className="flex flex-col items-center justify-center py-16 text-sm text-gray-400">
              <MessageSquareWarning size={32} className="opacity-30 mb-3" />
              <div>当前筛选条件下暂无负反馈归纳记录</div>
              <div className="text-xs mt-1">
                在「用例管理」里修改用例或带原因点踩后，系统会自动归纳并显示在这里
              </div>
            </div>
          )}

          {items.map(record => (
            <FeedbackCard key={record.id} record={record} />
          ))}
        </div>
      </main>
    </div>
  )
}
