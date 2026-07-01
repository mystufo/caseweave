import { useCallback, useEffect, useState } from 'react'
import {
  fetchPrompts, fetchPromptVersions, fetchPromptDefault,
  createPromptVersion, activatePromptVersion, resetPromptToDefault,
  type PromptSummary, type PromptVersionItem,
} from '../api/client'
import {
  X, Loader2, RefreshCw, Save, RotateCcw, Check, FileCog, ChevronLeft,
} from 'lucide-react'

interface Props {
  open: boolean
  onClose: () => void
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

  const activePrompt = prompts.find(p => p.key === activeKey) || null
  const dirty = draft !== draftBaseline

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
    void loadVersions(key)
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
      await createPromptVersion(activeKey, { template: draft, activate: true })
      setNotice('已保存为新版本并设为生效')
      await loadVersions(activeKey)
      await reloadPrompts()
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
                  {/* 编辑器 */}
                  <div className="flex-1 px-5 pb-4 min-w-0 flex flex-col">
                    <textarea
                      value={draft}
                      onChange={e => setDraft(e.target.value)}
                      spellCheck={false}
                      className="flex-1 w-full resize-none font-mono text-xs leading-relaxed p-3 border border-gray-200 rounded-md focus:outline-none focus:ring-2 focus:ring-teal-200 focus:border-teal-400"
                      placeholder="System Prompt 内容…"
                    />
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
