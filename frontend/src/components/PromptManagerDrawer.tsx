import { useCallback, useEffect, useState } from 'react'
import {
  fetchPrompts, fetchPromptVersions, fetchPromptDefault,
  createPromptVersion, activatePromptVersion, resetPromptToDefault,
  generatePromptSuggestion, fetchPromptSuggestions, dismissPromptSuggestion,
  type PromptSummary, type PromptVersionItem, type PromptSuggestion,
} from '../api/client'
import {
  X, Loader2, RefreshCw, Save, RotateCcw, Check, FileCog, ChevronLeft,
  Wand2, Lightbulb, ArrowRightLeft,
} from 'lucide-react'

const GENERATOR_KEY = 'generator'  // 本期唯一支持「改进建议」的 prompt

interface Props {
  open: boolean
  onClose: () => void
}

/**
 * 轻量行级 diff：把基线与建议按行 LCS 对齐，删除行标红、新增行标绿、未变行灰显。
 * 纯展示用，帮助人工一眼看清建议改了哪里；不追求 word-level 精度。
 */
function SuggestionDiff({ base, next }: { base: string; next: string }) {
  const a = base.split('\n')
  const b = next.split('\n')
  // LCS 表
  const m = a.length, n = b.length
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0))
  for (let i = m - 1; i >= 0; i--)
    for (let j = n - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
  const rows: { kind: 'ctx' | 'del' | 'add'; text: string }[] = []
  let i = 0, j = 0
  while (i < m && j < n) {
    if (a[i] === b[j]) { rows.push({ kind: 'ctx', text: a[i] }); i++; j++ }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { rows.push({ kind: 'del', text: a[i] }); i++ }
    else { rows.push({ kind: 'add', text: b[j] }); j++ }
  }
  while (i < m) { rows.push({ kind: 'del', text: a[i] }); i++ }
  while (j < n) { rows.push({ kind: 'add', text: b[j] }); j++ }

  const changed = rows.filter(r => r.kind !== 'ctx').length
  return (
    <div className="rounded border border-gray-200 bg-gray-50 max-h-56 overflow-auto">
      <div className="flex items-center gap-1 px-2 py-1 text-[10px] text-gray-500 border-b border-gray-100 bg-white/70 sticky top-0">
        <ArrowRightLeft size={10} /> 与当前生效版本对比（{changed} 行变化）
      </div>
      <pre className="font-mono text-[11px] leading-snug p-2 whitespace-pre-wrap break-words">
        {rows.map((r, idx) => (
          <div
            key={idx}
            className={
              r.kind === 'del' ? 'bg-red-50 text-red-700'
                : r.kind === 'add' ? 'bg-emerald-50 text-emerald-700'
                  : 'text-gray-400'
            }
          >
            <span className="select-none opacity-60">{r.kind === 'del' ? '- ' : r.kind === 'add' ? '+ ' : '  '}</span>
            {r.text || ' '}
          </div>
        ))}
      </pre>
    </div>
  )
}

/**
 * System Prompt 版本化管理抽屉（Phase 4.2 第一阶段）。
 *
 * 左栏：3 个可管理的 system prompt（澄清首轮 / 续答 / 用例生成），显示当前生效版本。
 * 右栏：选中某个后，展示编辑器 + 版本列表。可以：
 *   - 载入「原始建议版本」（代码默认）作为编辑基础
 *   - 保存为新版本（并设为生效）
 *   - 在历史版本间切换生效
 *   - 恢复默认（取消所有生效标记，回到用代码默认常量）
 */
export default function PromptManagerDrawer({ open, onClose }: Props) {
  const [prompts, setPrompts] = useState<PromptSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [activeKey, setActiveKey] = useState<string | null>(null)
  const [versions, setVersions] = useState<PromptVersionItem[]>([])
  const [versionsLoading, setVersionsLoading] = useState(false)
  const [draft, setDraft] = useState('')
  const [draftBaseline, setDraftBaseline] = useState('')   // 用于判断是否有未保存改动
  const [saving, setSaving] = useState(false)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // Phase 4.2 二阶段：generator 改进建议
  const [suggestions, setSuggestions] = useState<PromptSuggestion[]>([])
  const [suggestLoading, setSuggestLoading] = useState(false)   // 拉列表
  const [generating, setGenerating] = useState(false)           // 触发分析生成
  const [suggestMsg, setSuggestMsg] = useState<string | null>(null)
  // 当前编辑器内容源自哪条建议（采用后 setDraft 时记下，保存时回传 from_suggestion_id）
  const [adoptingId, setAdoptingId] = useState<number | null>(null)

  const activePrompt = prompts.find(p => p.key === activeKey) || null
  const dirty = draft !== draftBaseline
  const isGenerator = activeKey === GENERATOR_KEY

  const reloadPrompts = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setPrompts(await fetchPrompts())
    } catch {
      setError('加载提示词列表失败，请重试')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) {
      void reloadPrompts()
    } else {
      // 关闭时复位，避免下次打开残留旧状态
      setActiveKey(null)
      setVersions([])
      setDraft('')
      setDraftBaseline('')
      setError(null)
      setNotice(null)
    }
  }, [open, reloadPrompts])

  const loadVersions = useCallback(async (key: string) => {
    setVersionsLoading(true)
    try {
      const list = await fetchPromptVersions(key)
      setVersions(list)
      // 默认把当前生效版本载入编辑器；没有自定义版本则载入原始建议版本
      const active = list.find(v => v.is_active)
      if (active) {
        setDraft(active.template)
        setDraftBaseline(active.template)
      } else {
        const def = await fetchPromptDefault(key)
        setDraft(def.template)
        setDraftBaseline(def.template)
      }
    } catch {
      setError('加载版本失败，请重试')
    } finally {
      setVersionsLoading(false)
    }
  }, [])

  const openPrompt = (key: string) => {
    setActiveKey(key)
    setNotice(null)
    setError(null)
    setSuggestMsg(null)
    setSuggestions([])
    setAdoptingId(null)
    void loadVersions(key)
    if (key === GENERATOR_KEY) void loadSuggestions(key)
  }

  const loadSuggestions = useCallback(async (key: string) => {
    setSuggestLoading(true)
    try {
      setSuggestions(await fetchPromptSuggestions(key, 'pending'))
    } catch {
      // 建议是增益功能，拉取失败不打断主流程，仅静默
      setSuggestions([])
    } finally {
      setSuggestLoading(false)
    }
  }, [])

  const generateSuggestion = async () => {
    if (!activeKey) return
    setGenerating(true)
    setSuggestMsg(null)
    setError(null)
    try {
      const r = await generatePromptSuggestion(activeKey)
      if (r.created) {
        setSuggestMsg(`已生成 1 条改进建议（基于 ${r.feedback_count} 条负反馈）`)
        await loadSuggestions(activeKey)
      } else {
        setSuggestMsg(`未生成建议：${r.reason || '信号不足'}（负反馈样本 ${r.feedback_count} 条）`)
      }
    } catch {
      setSuggestMsg('生成建议失败，请稍后重试')
    } finally {
      setGenerating(false)
    }
  }

  // 采用建议：把建议全文灌进编辑器（人可继续改），保存时走既有版本化 API 并回传来源 id
  const adoptSuggestion = (s: PromptSuggestion) => {
    setDraft(s.suggested_template)
    setAdoptingId(s.id)
    setNotice('已把建议载入编辑器，可继续修改后点「保存为新版本」')
  }

  const dismiss = async (id: number) => {
    setBusyAction(`dismiss-${id}`)
    try {
      await dismissPromptSuggestion(id)
      setSuggestions(prev => prev.filter(s => s.id !== id))
      if (adoptingId === id) setAdoptingId(null)
    } catch {
      setError('忽略建议失败，请重试')
    } finally {
      setBusyAction(null)
    }
  }

  const loadDefaultIntoEditor = async () => {
    if (!activeKey) return
    try {
      const def = await fetchPromptDefault(activeKey)
      setDraft(def.template)
      setNotice('已载入原始建议版本（尚未保存）')
    } catch {
      setError('载入原始建议版本失败')
    }
  }

  const loadVersionIntoEditor = (v: PromptVersionItem) => {
    setDraft(v.template)
    setNotice(`已载入版本 v${v.version} 到编辑器（尚未保存）`)
  }

  const saveAsNewVersion = async () => {
    if (!activeKey || !draft.trim()) return
    setSaving(true)
    setError(null)
    setNotice(null)
    try {
      await createPromptVersion(activeKey, {
        template: draft,
        activate: true,
        from_suggestion_id: adoptingId ?? undefined,
      })
      setNotice('已保存为新版本并设为生效')
      setAdoptingId(null)
      await loadVersions(activeKey)
      await reloadPrompts()
      if (activeKey === GENERATOR_KEY) await loadSuggestions(activeKey)
    } catch {
      setError('保存失败，请重试')
    } finally {
      setSaving(false)
    }
  }

  const activate = async (versionId: number) => {
    if (!activeKey) return
    setBusyAction(`activate-${versionId}`)
    setError(null)
    try {
      await activatePromptVersion(activeKey, versionId)
      setNotice('已切换生效版本')
      await loadVersions(activeKey)
      await reloadPrompts()
    } catch {
      setError('切换生效版本失败')
    } finally {
      setBusyAction(null)
    }
  }

  const resetDefault = async () => {
    if (!activeKey) return
    setBusyAction('reset')
    setError(null)
    try {
      await resetPromptToDefault(activeKey)
      setNotice('已恢复使用原始建议版本')
      await loadVersions(activeKey)
      await reloadPrompts()
    } catch {
      setError('恢复默认失败')
    } finally {
      setBusyAction(null)
    }
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg shadow-xl w-[1140px] max-w-[96vw] h-[85vh] flex flex-col">
        {/* 头部 */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-gray-200">
          <div className="flex items-center gap-2 text-teal-700">
            <FileCog size={16} />
            <h3 className="text-base font-semibold">系统提示词管理</h3>
            <span className="text-xs text-gray-400">编辑、版本化与切换生效的 System Prompt（按项目隔离）</span>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-700 rounded"
          >
            <X size={16} />
          </button>
        </div>

        {(error || notice) && (
          <div className={`px-5 py-2 text-xs ${error ? 'text-red-600 bg-red-50' : 'text-teal-700 bg-teal-50'}`}>
            {error || notice}
          </div>
        )}

        <div className="flex-1 flex min-h-0">
          {/* 左栏：prompt 列表 */}
          <div className="w-64 flex-shrink-0 border-r border-gray-200 overflow-y-auto">
            <div className="flex items-center justify-between px-3 py-2">
              <span className="text-xs font-semibold text-gray-500">可管理的提示词</span>
              <button
                type="button"
                onClick={() => void reloadPrompts()}
                className="p-1 text-gray-400 hover:text-gray-700"
                title="刷新"
              >
                <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
              </button>
            </div>
            {loading && prompts.length === 0 ? (
              <div className="flex items-center justify-center py-8 text-gray-400">
                <Loader2 size={16} className="animate-spin" />
              </div>
            ) : (
              <ul className="pb-3">
                {prompts.map(p => {
                  const selected = p.key === activeKey
                  return (
                    <li key={p.key}>
                      <button
                        type="button"
                        onClick={() => openPrompt(p.key)}
                        className={`w-full text-left px-3 py-2.5 border-l-2 transition ${
                          selected
                            ? 'border-teal-500 bg-teal-50'
                            : 'border-transparent hover:bg-gray-50'
                        }`}
                      >
                        <div className="text-sm font-medium text-gray-800">{p.label}</div>
                        <div className="text-[11px] text-gray-500 mt-0.5 line-clamp-2">{p.description}</div>
                        <div className="mt-1 flex items-center gap-1.5">
                          {p.using_default ? (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500">
                              使用原始建议版本
                            </span>
                          ) : (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-teal-100 text-teal-700">
                              生效 v{p.active_version}
                            </span>
                          )}
                          <span className="text-[10px] text-gray-400">{p.version_count} 个版本</span>
                        </div>
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>

          {/* 右栏：编辑器 + 版本列表 */}
          <div className="flex-1 flex flex-col min-w-0">
            {!activePrompt ? (
              <div className="flex-1 flex flex-col items-center justify-center text-gray-400 gap-2">
                <ChevronLeft size={20} />
                <span className="text-sm">从左侧选择一个提示词开始编辑</span>
              </div>
            ) : (
              <>
                <div className="px-5 pt-3 pb-2">
                  <div className="flex items-center justify-between">
                    <h4 className="text-sm font-semibold text-gray-800">{activePrompt.label}</h4>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => void loadDefaultIntoEditor()}
                        className="inline-flex items-center gap-1 px-2 py-1 text-xs text-gray-600 border border-gray-300 rounded hover:bg-gray-50"
                        title="把代码内置的原始建议版本载入编辑器"
                      >
                        <RotateCcw size={12} />
                        载入原始建议版本
                      </button>
                      <button
                        type="button"
                        onClick={() => void saveAsNewVersion()}
                        disabled={saving || !dirty || !draft.trim()}
                        className="inline-flex items-center gap-1 px-2 py-1 text-xs bg-teal-600 text-white rounded hover:bg-teal-700 disabled:opacity-50"
                        title={dirty ? '保存为新版本并设为生效' : '内容无改动'}
                      >
                        {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
                        保存为新版本
                      </button>
                    </div>
                  </div>
                  {dirty && (
                    <div className="text-[11px] text-amber-600 mt-1">编辑器有未保存的改动</div>
                  )}
                </div>

                <div className="flex-1 min-h-0 flex">
                  {/* 编辑器 + 改进建议 */}
                  <div className="flex-1 px-5 pb-4 min-w-0 flex flex-col gap-3">
                    <textarea
                      value={draft}
                      onChange={e => setDraft(e.target.value)}
                      spellCheck={false}
                      className="flex-1 w-full resize-none font-mono text-xs leading-relaxed p-3 border border-gray-200 rounded-md focus:outline-none focus:ring-2 focus:ring-teal-200 focus:border-teal-400"
                      placeholder="System Prompt 内容…"
                    />

                    {/* 改进建议（仅 generator）：系统分析负反馈产出的草稿，人工审核后采用 */}
                    {isGenerator && (
                      <div className="flex-shrink-0 max-h-[40%] overflow-y-auto border border-indigo-100 rounded-md bg-indigo-50/40">
                        <div className="flex items-center justify-between px-3 py-2 border-b border-indigo-100 bg-white/60 sticky top-0">
                          <div className="flex items-center gap-1.5 text-xs font-semibold text-indigo-700">
                            <Lightbulb size={13} />
                            改进建议（{suggestions.length}）
                            <span className="font-normal text-[11px] text-indigo-400">
                              系统分析负反馈生成，采用需人工确认
                            </span>
                          </div>
                          <button
                            type="button"
                            onClick={() => void generateSuggestion()}
                            disabled={generating}
                            className="inline-flex items-center gap-1 px-2 py-1 text-[11px] bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
                            title="分析本项目对用例生成的负反馈，产出一条 prompt 改进建议草稿"
                          >
                            {generating ? <Loader2 size={11} className="animate-spin" /> : <Wand2 size={11} />}
                            分析负反馈生成建议
                          </button>
                        </div>
                        <div className="p-3 space-y-2">
                          {suggestMsg && (
                            <div className="text-[11px] text-indigo-700 bg-indigo-100/60 rounded px-2 py-1">
                              {suggestMsg}
                            </div>
                          )}
                          {suggestLoading ? (
                            <div className="flex items-center gap-2 text-[11px] text-gray-400 py-1">
                              <Loader2 size={11} className="animate-spin" /> 加载建议…
                            </div>
                          ) : suggestions.length === 0 ? (
                            <div className="text-[11px] text-gray-400 py-1">
                              暂无待审核建议。点右上按钮基于负反馈生成一条。
                            </div>
                          ) : (
                            suggestions.map(s => (
                              <div key={s.id} className="rounded border border-indigo-200 bg-white p-2.5">
                                {s.rationale && (
                                  <div className="text-xs text-gray-700 mb-1.5">
                                    <span className="font-medium text-indigo-700">改动理由：</span>
                                    {s.rationale}
                                  </div>
                                )}
                                {s.evidence?.feedback_count != null && (
                                  <div className="text-[11px] text-gray-400 mb-1.5">
                                    证据：引用 {s.evidence.feedback_count} 条负反馈
                                    {s.evidence.samples && s.evidence.samples.length > 0 && (
                                      <> · {Array.from(new Set(s.evidence.samples.map(x => x.intent).filter(Boolean))).join(' / ')}</>
                                    )}
                                  </div>
                                )}
                                <SuggestionDiff base={s.base_template} next={s.suggested_template} />
                                <div className="flex items-center gap-2 mt-2">
                                  <button
                                    type="button"
                                    onClick={() => adoptSuggestion(s)}
                                    className="inline-flex items-center gap-1 px-2 py-1 text-[11px] bg-teal-600 text-white rounded hover:bg-teal-700"
                                    title="把建议全文载入上方编辑器（可继续改），再点「保存为新版本」生效"
                                  >
                                    <Check size={11} /> 采用（载入编辑器）
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => void dismiss(s.id)}
                                    disabled={busyAction === `dismiss-${s.id}`}
                                    className="inline-flex items-center gap-1 px-2 py-1 text-[11px] text-gray-500 border border-gray-200 rounded hover:bg-gray-50 disabled:opacity-50"
                                  >
                                    {busyAction === `dismiss-${s.id}` ? <Loader2 size={11} className="animate-spin" /> : <X size={11} />}
                                    忽略
                                  </button>
                                </div>
                              </div>
                            ))
                          )}
                        </div>
                      </div>
                    )}
                  </div>

                  {/* 版本历史（右侧竖栏） */}
                  <div className="w-72 flex-shrink-0 border-l border-gray-100 flex flex-col min-h-0">
                    <div className="flex items-center justify-between px-4 py-2.5 flex-shrink-0">
                      <span className="text-xs font-semibold text-gray-500">
                        历史版本（{versions.length}）
                      </span>
                      <button
                        type="button"
                        onClick={() => void resetDefault()}
                        disabled={busyAction === 'reset' || activePrompt.using_default}
                        className="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] text-gray-500 hover:text-gray-800 disabled:opacity-40"
                        title="取消所有生效标记，恢复使用原始建议版本（不删除历史版本）"
                      >
                        <RotateCcw size={11} />
                        恢复默认
                      </button>
                    </div>
                    <div className="flex-1 overflow-y-auto px-4 pb-3 min-h-0">
                      {versionsLoading ? (
                        <div className="flex items-center justify-center py-4 text-gray-400">
                          <Loader2 size={14} className="animate-spin" />
                        </div>
                      ) : versions.length === 0 ? (
                        <div className="text-xs text-gray-400 py-2">
                          还没有自定义版本，当前使用代码内置的原始建议版本。
                        </div>
                      ) : (
                        <ul className="space-y-1">
                          {versions.map(v => (
                            <li
                              key={v.id}
                              className="px-2 py-1.5 rounded hover:bg-gray-50"
                            >
                              <div className="flex items-center gap-2">
                                <span className="text-xs font-medium text-gray-700">v{v.version}</span>
                                {v.is_active && (
                                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-teal-100 text-teal-700 inline-flex items-center gap-0.5">
                                    <Check size={10} /> 生效中
                                  </span>
                                )}
                              </div>
                              <div className="text-[11px] text-gray-400 mt-0.5 truncate">
                                {v.created_at ? new Date(v.created_at).toLocaleString('zh-CN') : ''}
                              </div>
                              <div className="flex items-center gap-3 mt-1">
                                <button
                                  type="button"
                                  onClick={() => loadVersionIntoEditor(v)}
                                  className="text-[11px] text-gray-500 hover:text-teal-700"
                                  title="载入该版本到编辑器"
                                >
                                  载入
                                </button>
                                {!v.is_active && (
                                  <button
                                    type="button"
                                    onClick={() => void activate(v.id)}
                                    disabled={busyAction === `activate-${v.id}`}
                                    className="text-[11px] text-teal-600 hover:text-teal-800 disabled:opacity-50"
                                    title="设为生效版本"
                                  >
                                    {busyAction === `activate-${v.id}` ? '切换中…' : '设为生效'}
                                  </button>
                                )}
                              </div>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
