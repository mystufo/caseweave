import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  fetchModules, createModule, updateModule, deleteModule,
  fetchProjectKnowledge, updateKnowledge, deleteKnowledge,
  fetchModuleRelations, createModuleRelation, deleteModuleRelation,
  fetchKnowledgeStats,
  fetchSkills, fetchSkillDetail, createSkill, updateSkill, deleteSkill, regenerateSkill,
  fetchRecentFeedback,
  fetchDocuments, fetchDocumentDetail, updateDocumentModule,
  type KnowledgeHit, type ModuleSummary, type ModuleRelation, type ModuleRelationType,
  type KnowledgeStats,
  type SkillSummary, type RecentFeedbackItem,
  type DocumentSummary, type DocumentDetail,
} from '../api/client'
import TabBar, { type ViewKey } from '../components/TabBar'
import PromptManagerDrawer from '../components/PromptManagerDrawer'
import {
  Search, RefreshCw, Loader2, Edit2, Check, X, Trash2, BookOpen, Plus, Network,
  BarChart3, Sparkles, FileText, Brain, Wand2, History, FileCog,
  Eye, ExternalLink, Save, Layers,
} from 'lucide-react'

interface PageProps {
  view: ViewKey
  onChangeView: (v: ViewKey) => void
}

const TYPE_LABEL: Record<string, string> = {
  product_rule: '产品规则',
  module_relation: '模块关系',
  defect_pattern: '缺陷模式',
  term: '术语',
  constraint: '约束',
  ui_behavior: 'UI 行为',
}

const TYPE_BADGE: Record<string, string> = {
  product_rule: 'bg-blue-100 text-blue-700 border-blue-200',
  module_relation: 'bg-purple-100 text-purple-700 border-purple-200',
  defect_pattern: 'bg-red-100 text-red-700 border-red-200',
  term: 'bg-gray-100 text-gray-700 border-gray-200',
  constraint: 'bg-amber-100 text-amber-700 border-amber-200',
  ui_behavior: 'bg-emerald-100 text-emerald-700 border-emerald-200',
}

const ALL = -1  // 模块过滤的"全部"哨兵——后端 module_id 用 null 表示不过滤
const ORPHAN = -2  // 项目级（module_id = NULL）的条目

type EditDraft = { content: string; confidence: number }

const RELATION_LABEL: Record<ModuleRelationType, string> = {
  depends_on: '依赖',
  triggers: '触发',
  shares_data: '共享数据',
  blocks: '阻塞',
  extends: '扩展',
}
const RELATION_TYPES: ModuleRelationType[] = ['depends_on', 'triggers', 'shares_data', 'blocks', 'extends']

function formatDate(iso: string | null): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

const FILE_TYPE_LABEL: Record<string, string> = {
  docx: 'Word',
  pdf: 'PDF',
  lark_doc: '飞书文档',
  lark_docs: '飞书文档',
  lark_wiki: '飞书知识库',
  lark_sheet: '飞书表格',
  mindmap_md: '脑图',
}

/**
 * 模块详情卡：选中某个真实模块时，聚合展示并编辑该模块名下的
 * 需求文档 / 脑图 / Skill，以及模块自身的名称与描述。
 *
 * 文档/脑图自带数据获取（按 moduleId 拉取）；Skill 编辑复用父级的
 * Skills 抽屉（通过 onManageSkills / onEditSkill 回调）。
 */
function ModuleDetailCard({
  module,
  allModules,
  onModuleUpdated,
  onModuleDeleted,
  onManageSkills,
  onEditSkill,
}: {
  module: ModuleSummary
  allModules: ModuleSummary[]
  onModuleUpdated: (m: ModuleSummary) => void
  onModuleDeleted: (id: number) => void
  onManageSkills: () => void
  onEditSkill: (skill: SkillSummary) => void
}) {
  const [docs, setDocs] = useState<DocumentSummary[]>([])
  const [skills, setSkills] = useState<SkillSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  // 模块信息内联编辑
  const [editing, setEditing] = useState(false)
  const [nameDraft, setNameDraft] = useState(module.name)
  const [codeDraft, setCodeDraft] = useState(module.code || '')
  const [descDraft, setDescDraft] = useState(module.description || '')
  const [savingModule, setSavingModule] = useState(false)
  const [confirmDel, setConfirmDel] = useState(false)
  const [deleting, setDeleting] = useState(false)

  // 文档归属调整 / 查看
  const [reassigning, setReassigning] = useState<number | null>(null)
  const [viewing, setViewing] = useState<DocumentDetail | null>(null)
  const [viewLoading, setViewLoading] = useState(false)

  const reload = useCallback(async () => {
    setLoading(true)
    setErr(null)
    try {
      const [d, allSkills] = await Promise.all([
        fetchDocuments({ moduleId: module.id }),
        fetchSkills(),
      ])
      setDocs(d)
      setSkills(allSkills.filter(s => s.module_id === module.id))
    } catch (e) {
      console.error('Module detail load failed:', e)
      setErr('加载模块资产失败，请重试')
    } finally {
      setLoading(false)
    }
  }, [module.id])

  useEffect(() => {
    setEditing(false)
    setConfirmDel(false)
    setNameDraft(module.name)
    setCodeDraft(module.code || '')
    setDescDraft(module.description || '')
    void reload()
  }, [module.id, module.name, module.code, module.description, reload])

  const prdDocs = docs.filter(d => d.role !== 'mindmap')
  const mindmapDocs = docs.filter(d => d.role === 'mindmap')

  const codeOk = codeDraft.trim() === '' || /^[A-Z][A-Z0-9-]{0,39}$/.test(codeDraft.trim())

  const saveModule = async () => {
    const name = nameDraft.trim()
    if (!name || !codeOk) return
    setSavingModule(true)
    setErr(null)
    try {
      const updated = await updateModule(module.id, {
        name,
        code: codeDraft.trim() || null,
        description: descDraft.trim() || null,
      })
      onModuleUpdated(updated)
      setEditing(false)
    } catch (e) {
      console.error('Update module failed:', e)
      setErr('保存模块失败，名称可能已存在')
    } finally {
      setSavingModule(false)
    }
  }

  const doDeleteModule = async () => {
    setDeleting(true)
    try {
      await deleteModule(module.id)
      onModuleDeleted(module.id)
    } catch (e) {
      console.error('Delete module failed:', e)
      setErr('删除模块失败')
      setDeleting(false)
    }
  }

  const reassignDoc = async (docId: number, target: number | null) => {
    setReassigning(docId)
    try {
      await updateDocumentModule(docId, target)
      // 改到别的模块后，本卡片不再持有它 → 直接从列表移除
      setDocs(prev => prev.filter(d => d.id !== docId))
    } catch (e) {
      console.error('Reassign document failed:', e)
      setErr('调整文档归属失败')
    } finally {
      setReassigning(null)
    }
  }

  const viewDoc = async (docId: number) => {
    setViewLoading(true)
    try {
      setViewing(await fetchDocumentDetail(docId))
    } catch (e) {
      console.error('Load document detail failed:', e)
      setErr('加载文档内容失败')
    } finally {
      setViewLoading(false)
    }
  }

  const renderDocRow = (d: DocumentSummary) => (
    <div
      key={d.id}
      className="flex items-center gap-2 px-3 py-2 bg-white border border-gray-100 rounded hover:border-gray-200"
    >
      <FileText size={13} className="text-gray-400 flex-shrink-0" />
      <span className="text-sm text-gray-800 truncate flex-1" title={d.filename}>{d.filename}</span>
      <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 whitespace-nowrap">
        {FILE_TYPE_LABEL[d.file_type] || d.file_type}
      </span>
      {d.source_url && (
        <a
          href={d.source_url}
          target="_blank"
          rel="noreferrer"
          className="text-gray-400 hover:text-blue-600"
          title="打开飞书原文"
        >
          <ExternalLink size={13} />
        </a>
      )}
      <button
        type="button"
        onClick={() => void viewDoc(d.id)}
        className="inline-flex items-center gap-1 text-[11px] text-gray-500 hover:text-amber-700 px-1"
        title="查看解析正文"
      >
        <Eye size={13} /> 查看
      </button>
      <select
        value=""
        disabled={reassigning === d.id}
        onChange={e => {
          const v = e.target.value
          void reassignDoc(d.id, v === '__none__' ? null : Number(v))
        }}
        className="text-[11px] border border-gray-200 rounded px-1 py-0.5 text-gray-600 focus:outline-none focus:ring-1 focus:ring-amber-300 disabled:opacity-50"
        title="改归属模块"
      >
        <option value="" disabled>改归属…</option>
        {allModules.filter(m => m.id !== module.id).map(m => (
          <option key={m.id} value={m.id}>移到「{m.name}」</option>
        ))}
        <option value="__none__">取消归属</option>
      </select>
    </div>
  )

  return (
    <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50/30 overflow-hidden">
      {/* 模块信息头 */}
      <div className="px-4 py-3 border-b border-amber-100 bg-white/60">
        {editing ? (
          <div className="space-y-2">
            <input
              type="text"
              value={nameDraft}
              onChange={e => setNameDraft(e.target.value)}
              placeholder="模块中文名"
              disabled={savingModule}
              className="w-full px-2 py-1 text-sm font-medium border border-gray-200 rounded focus:outline-none focus:ring-2 focus:ring-amber-200"
              autoFocus
            />
            <input
              type="text"
              value={codeDraft}
              onChange={e => setCodeDraft(e.target.value.toUpperCase())}
              placeholder="英文名 / 用例编号前缀（如 ORDER-MGMT）"
              disabled={savingModule}
              className={`w-full px-2 py-1 text-xs font-mono border rounded focus:outline-none focus:ring-2 ${
                codeOk ? 'border-gray-200 focus:ring-amber-200' : 'border-red-300 focus:ring-red-200'
              }`}
            />
            {!codeOk && (
              <div className="text-[11px] text-red-500">英文名须大写字母开头，仅含 A–Z 0–9 和短横线</div>
            )}
            <textarea
              value={descDraft}
              onChange={e => setDescDraft(e.target.value)}
              placeholder="模块描述（可选）"
              rows={2}
              disabled={savingModule}
              className="w-full px-2 py-1 text-xs border border-gray-200 rounded resize-none focus:outline-none focus:ring-2 focus:ring-amber-200"
            />
            <div className="flex gap-1.5">
              <button
                type="button"
                onClick={() => void saveModule()}
                disabled={savingModule || !nameDraft.trim() || !codeOk}
                className="inline-flex items-center gap-1 px-2 py-1 text-xs bg-amber-600 text-white rounded hover:bg-amber-700 disabled:opacity-50"
              >
                {savingModule ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
                保存
              </button>
              <button
                type="button"
                onClick={() => { setEditing(false); setNameDraft(module.name); setCodeDraft(module.code || ''); setDescDraft(module.description || '') }}
                disabled={savingModule}
                className="px-2 py-1 text-xs text-gray-500 border border-gray-200 rounded hover:bg-gray-50"
              >
                取消
              </button>
            </div>
          </div>
        ) : (
          <div className="flex items-start gap-2">
            <Layers size={16} className="text-amber-600 mt-0.5 flex-shrink-0" />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold text-gray-800">{module.name}</span>
                {module.code && (
                  <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-amber-100 text-amber-700" title="英文名 / 用例编号前缀">
                    {module.code}
                  </span>
                )}
              </div>
              {module.description && (
                <div className="text-xs text-gray-500 mt-0.5">{module.description}</div>
              )}
            </div>
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="p-1 text-gray-400 hover:text-amber-700"
              title="编辑模块信息"
            >
              <Edit2 size={13} />
            </button>
            {confirmDel ? (
              <div className="inline-flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => void doDeleteModule()}
                  disabled={deleting}
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[11px] bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50"
                  title="确认删除（名下文档/Skill 将变为未分类，不会被删除）"
                >
                  {deleting ? <Loader2 size={11} className="animate-spin" /> : <Trash2 size={11} />}
                  确认删除
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmDel(false)}
                  disabled={deleting}
                  className="px-1.5 py-0.5 text-[11px] text-gray-500 border border-gray-200 rounded hover:bg-gray-50"
                >
                  取消
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmDel(true)}
                className="p-1 text-gray-400 hover:text-red-600"
                title="删除模块"
              >
                <Trash2 size={13} />
              </button>
            )}
          </div>
        )}
      </div>

      {err && (
        <div className="px-4 py-2 text-xs text-red-700 bg-red-50 border-b border-red-100">{err}</div>
      )}

      {/* 资产区 */}
      <div className="p-4 space-y-4">
        {loading ? (
          <div className="flex items-center justify-center py-6 text-xs text-gray-400">
            <Loader2 size={14} className="animate-spin mr-2" /> 加载模块资产…
          </div>
        ) : (
          <>
            {/* 需求文档 */}
            <section>
              <div className="text-xs font-semibold text-gray-500 mb-1.5">
                需求文档（{prdDocs.length}）
              </div>
              {prdDocs.length === 0 ? (
                <div className="text-xs text-gray-400 py-1">该模块暂无需求文档</div>
              ) : (
                <div className="space-y-1.5">{prdDocs.map(renderDocRow)}</div>
              )}
            </section>

            {/* 脑图 */}
            <section>
              <div className="text-xs font-semibold text-gray-500 mb-1.5">
                测试脑图（{mindmapDocs.length}）
              </div>
              {mindmapDocs.length === 0 ? (
                <div className="text-xs text-gray-400 py-1">该模块暂无脑图</div>
              ) : (
                <div className="space-y-1.5">{mindmapDocs.map(renderDocRow)}</div>
              )}
            </section>

            {/* Skill */}
            <section>
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-xs font-semibold text-gray-500">
                  测试设计经验 Skill（{skills.length}）
                </span>
                <button
                  type="button"
                  onClick={onManageSkills}
                  className="inline-flex items-center gap-1 text-[11px] text-indigo-600 hover:text-indigo-800"
                  title="打开测试设计经验抽屉（新建 / 自动归纳）"
                >
                  <Brain size={12} /> 管理
                </button>
              </div>
              {skills.length === 0 ? (
                <div className="text-xs text-gray-400 py-1">该模块暂无 Skill</div>
              ) : (
                <div className="space-y-1.5">
                  {skills.map(s => (
                    <div
                      key={s.id}
                      className="flex items-center gap-2 px-3 py-2 bg-white border border-gray-100 rounded hover:border-gray-200"
                    >
                      <Brain size={13} className="text-indigo-400 flex-shrink-0" />
                      <span className="text-sm text-gray-800 truncate flex-1">{s.name}</span>
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 whitespace-nowrap">
                        {s.source === 'auto_generated' ? '自动归纳' : '人工'} · v{s.version}
                      </span>
                      <button
                        type="button"
                        onClick={() => onEditSkill(s)}
                        className="inline-flex items-center gap-1 text-[11px] text-gray-500 hover:text-indigo-700 px-1"
                        title="编辑该 Skill"
                      >
                        <Edit2 size={12} /> 编辑
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </>
        )}
      </div>

      {/* 文档正文只读预览 */}
      {(viewing || viewLoading) && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setViewing(null)}>
          <div
            className="bg-white rounded-lg shadow-xl w-[760px] max-w-[92vw] h-[80vh] flex flex-col"
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-5 py-3 border-b border-gray-200">
              <div className="flex items-center gap-2 min-w-0">
                <FileText size={15} className="text-amber-600 flex-shrink-0" />
                <h3 className="text-sm font-semibold text-gray-800 truncate">
                  {viewing?.filename || '加载中…'}
                </h3>
              </div>
              <button type="button" onClick={() => setViewing(null)} className="p-1 text-gray-400 hover:text-gray-700">
                <X size={16} />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-5 py-4">
              {viewLoading ? (
                <div className="flex items-center justify-center py-16 text-sm text-gray-400">
                  <Loader2 size={14} className="animate-spin mr-2" /> 加载中…
                </div>
              ) : (
                <pre className="whitespace-pre-wrap break-words font-sans text-xs leading-relaxed text-gray-700">
                  {viewing?.content || '（无解析正文）'}
                </pre>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default function KnowledgePage({ view, onChangeView }: PageProps) {
  const [modules, setModules] = useState<ModuleSummary[]>([])
  const [activeModule, setActiveModule] = useState<number>(ALL)
  const [items, setItems] = useState<KnowledgeHit[]>([])
  const [loading, setLoading] = useState(false)
  const [searchInput, setSearchInput] = useState('')
  const [query, setQuery] = useState('')   // 实际下发的搜索词（点搜索/回车后）
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editDraft, setEditDraft] = useState<EditDraft | null>(null)
  const [savingId, setSavingId] = useState<number | null>(null)
  const [deletingId, setDeletingId] = useState<number | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<KnowledgeHit | null>(null)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  // 新建模块表单
  const [newModuleName, setNewModuleName] = useState('')
  const [newModuleDesc, setNewModuleDesc] = useState('')
  const [creatingModule, setCreatingModule] = useState(false)
  const [moduleFormOpen, setModuleFormOpen] = useState(false)

  // 模块关联关系抽屉
  const [relationDialogOpen, setRelationDialogOpen] = useState(false)
  const [relations, setRelations] = useState<ModuleRelation[]>([])
  const [relationsLoading, setRelationsLoading] = useState(false)
  const [newRelationTarget, setNewRelationTarget] = useState<number | ''>('')
  const [newRelationType, setNewRelationType] = useState<ModuleRelationType>('depends_on')
  const [newRelationDesc, setNewRelationDesc] = useState('')
  const [submittingRelation, setSubmittingRelation] = useState(false)

  // 知识库总览统计（KnowledgePage header dashboard）
  const [stats, setStats] = useState<KnowledgeStats | null>(null)
  const [statsLoading, setStatsLoading] = useState(false)
  const reloadStats = useCallback(async () => {
    setStatsLoading(true)
    try {
      const s = await fetchKnowledgeStats()
      setStats(s)
    } catch (err) {
      console.error('Knowledge stats failed:', err)
    } finally {
      setStatsLoading(false)
    }
  }, [])

  // Phase 4: Skills 抽屉
  const [skillsDialogOpen, setSkillsDialogOpen] = useState(false)
  const [skills, setSkills] = useState<SkillSummary[]>([])
  const [skillsLoading, setSkillsLoading] = useState(false)

  // Phase 4.2: 系统提示词管理抽屉
  const [promptDrawerOpen, setPromptDrawerOpen] = useState(false)
  const [skillForm, setSkillForm] = useState<{
    id?: number; name: string; content: string;
  } | null>(null)
  const [savingSkill, setSavingSkill] = useState(false)
  const [deletingSkillId, setDeletingSkillId] = useState<number | null>(null)
  const [regenLoading, setRegenLoading] = useState(false)
  const [regenMessage, setRegenMessage] = useState<string | null>(null)

  // Phase 4: 最近修改沉淀
  const [recentFeedback, setRecentFeedback] = useState<RecentFeedbackItem[]>([])
  const [recentFeedbackLoading, setRecentFeedbackLoading] = useState(false)
  const reloadRecentFeedback = useCallback(async (moduleId: number | null) => {
    setRecentFeedbackLoading(true)
    try {
      const items = await fetchRecentFeedback({
        moduleId: moduleId ?? undefined,
        limit: 8,
      })
      setRecentFeedback(items)
    } catch (err) {
      console.error('recent feedback failed:', err)
      setRecentFeedback([])
    } finally {
      setRecentFeedbackLoading(false)
    }
  }, [])

  const moduleNameById = useMemo(() => {
    const m = new Map<number, string>()
    for (const x of modules) m.set(x.id, x.name)
    return m
  }, [modules])

  const reload = useCallback(async () => {
    setLoading(true)
    setErrorMsg(null)
    try {
      const moduleId =
        activeModule === ALL ? undefined : activeModule === ORPHAN ? null : activeModule
      const data = await fetchProjectKnowledge({
        q: query || undefined,
        moduleId,
        topK: query ? 50 : 200,
      })
      setItems(data)
    } catch (e) {
      console.error('Knowledge load failed:', e)
      setErrorMsg('加载失败，请稍后重试')
      setItems([])
    } finally {
      setLoading(false)
    }
  }, [activeModule, query])

  useEffect(() => {
    fetchModules().then(setModules).catch(err => console.error('Modules load:', err))
    void reloadStats()
  }, [reloadStats])

  useEffect(() => {
    void reload()
  }, [reload])

  // 切换模块时重新拉"最近修改沉淀"卡
  useEffect(() => {
    const realModuleId =
      activeModule === ALL || activeModule === ORPHAN ? null : activeModule
    void reloadRecentFeedback(realModuleId)
  }, [activeModule, reloadRecentFeedback])

  const startEdit = (item: KnowledgeHit) => {
    setEditingId(item.id)
    setEditDraft({ content: item.content, confidence: item.confidence })
  }
  const cancelEdit = () => {
    setEditingId(null)
    setEditDraft(null)
  }
  const saveEdit = async (id: number) => {
    if (!editDraft) return
    const cur = items.find(x => x.id === id)
    if (!cur) return
    const patch: { content?: string; confidence?: number } = {}
    if (editDraft.content !== cur.content) patch.content = editDraft.content
    if (editDraft.confidence !== cur.confidence) patch.confidence = editDraft.confidence
    if (Object.keys(patch).length === 0) {
      cancelEdit()
      return
    }
    setSavingId(id)
    try {
      const result = await updateKnowledge(id, patch)
      setItems(prev => prev.map(x => (x.id === id ? {
        ...x,
        content: editDraft.content,
        confidence: editDraft.confidence,
        version: result.version,
      } : x)))
      cancelEdit()
    } catch (e) {
      console.error('Update knowledge failed:', e)
      setErrorMsg('保存失败，请重试')
    } finally {
      setSavingId(null)
    }
  }

  // 当前选中的"真实"模块（不是 ALL/ORPHAN 哨兵）——决定关联关系抽屉是否可用
  const activeRealModule = useMemo(() => {
    if (activeModule === ALL || activeModule === ORPHAN) return null
    return modules.find(m => m.id === activeModule) || null
  }, [activeModule, modules])

  const submitNewModule = async () => {
    const name = newModuleName.trim()
    if (!name || creatingModule) return
    setCreatingModule(true)
    try {
      await createModule({ name, description: newModuleDesc.trim() || null })
      const fresh = await fetchModules()
      setModules(fresh)
      setNewModuleName('')
      setNewModuleDesc('')
      setModuleFormOpen(false)
    } catch (e) {
      console.error('Create module failed:', e)
      setErrorMsg('创建模块失败，名称是否已存在？')
    } finally {
      setCreatingModule(false)
    }
  }

  const reloadRelations = useCallback(async (moduleId: number) => {
    setRelationsLoading(true)
    try {
      const rs = await fetchModuleRelations(moduleId)
      setRelations(rs)
    } catch (e) {
      console.error('Load relations failed:', e)
      setRelations([])
    } finally {
      setRelationsLoading(false)
    }
  }, [])

  const openRelationDialog = () => {
    if (!activeRealModule) return
    setNewRelationTarget('')
    setNewRelationType('depends_on')
    setNewRelationDesc('')
    setRelationDialogOpen(true)
    void reloadRelations(activeRealModule.id)
  }

  const submitNewRelation = async () => {
    if (!activeRealModule) return
    if (!newRelationTarget || newRelationTarget === activeRealModule.id) return
    setSubmittingRelation(true)
    try {
      await createModuleRelation({
        source_module_id: activeRealModule.id,
        target_module_id: Number(newRelationTarget),
        relation_type: newRelationType,
        description: newRelationDesc.trim() || null,
      })
      setNewRelationTarget('')
      setNewRelationDesc('')
      await reloadRelations(activeRealModule.id)
    } catch (e) {
      console.error('Create relation failed:', e)
      setErrorMsg('创建关联关系失败，请重试')
    } finally {
      setSubmittingRelation(false)
    }
  }

  const removeRelation = async (relationId: number) => {
    if (!activeRealModule) return
    try {
      await deleteModuleRelation(relationId)
      await reloadRelations(activeRealModule.id)
    } catch (e) {
      console.error('Delete relation failed:', e)
    }
  }

  // ── Skills 抽屉 handlers ──────────────────────────────────────────────
  const reloadSkills = useCallback(async () => {
    setSkillsLoading(true)
    try {
      const all = await fetchSkills()
      setSkills(all)
    } catch (e) {
      console.error('Load skills failed:', e)
      setSkills([])
    } finally {
      setSkillsLoading(false)
    }
  }, [])

  const openSkillsDialog = () => {
    setSkillForm(null)
    setRegenMessage(null)
    setSkillsDialogOpen(true)
    void reloadSkills()
  }

  const startNewSkill = () => {
    setSkillForm({ name: '', content: '' })
  }

  const startEditSkill = async (skill: SkillSummary) => {
    try {
      const detail = await fetchSkillDetail(skill.id)
      setSkillForm({ id: skill.id, name: detail.name, content: detail.content || '' })
    } catch (e) {
      console.error('Load skill detail failed:', e)
      setErrorMsg('加载 Skill 内容失败')
    }
  }

  const submitSkillForm = async () => {
    if (!skillForm) return
    const name = skillForm.name.trim()
    const content = skillForm.content.trim()
    if (!name || !content) {
      setErrorMsg('Skill 名称和内容都不能为空')
      return
    }
    setSavingSkill(true)
    try {
      const moduleId = activeRealModule ? activeRealModule.id : null
      if (skillForm.id) {
        await updateSkill(skillForm.id, { name, content })
      } else {
        await createSkill({ name, content, module_id: moduleId })
      }
      setSkillForm(null)
      await reloadSkills()
    } catch (e) {
      console.error('Save skill failed:', e)
      setErrorMsg('保存 Skill 失败')
    } finally {
      setSavingSkill(false)
    }
  }

  const removeSkill = async (skillId: number) => {
    setDeletingSkillId(skillId)
    try {
      await deleteSkill(skillId)
      await reloadSkills()
    } catch (e) {
      console.error('Delete skill failed:', e)
      setErrorMsg('删除 Skill 失败')
    } finally {
      setDeletingSkillId(null)
    }
  }

  const triggerRegenerate = async () => {
    if (!activeRealModule) return
    setRegenLoading(true)
    setRegenMessage(null)
    try {
      const r = await regenerateSkill(activeRealModule.id)
      if (r.created) {
        setRegenMessage(
          `已${r.action === 'updated' ? '更新' : '创建'}「${activeRealModule.name}」的自动归纳 Skill。`
          + ` 样本：${r.feedback_count} 条反馈 + ${r.knowledge_count} 条知识。`,
        )
      } else {
        setRegenMessage(
          `未生成 Skill：${r.reason || '信号不足'}。当前样本：${r.feedback_count} 条反馈 + ${r.knowledge_count} 条知识。`,
        )
      }
      await reloadSkills()
    } catch (e) {
      console.error('Regenerate skill failed:', e)
      setRegenMessage('自动归纳失败，请稍后重试')
    } finally {
      setRegenLoading(false)
    }
  }

  // 当前抽屉里要展示的 skills：当前选中模块 + 项目级（module_id=null）
  const visibleSkills = useMemo(() => {
    if (!skillsDialogOpen) return []
    const realId = activeRealModule?.id ?? null
    return skills.filter(s => s.module_id === realId || s.module_id === null)
  }, [skills, activeRealModule, skillsDialogOpen])

  const doDelete = async (id: number) => {
    setDeletingId(id)
    try {
      await deleteKnowledge(id)
      setItems(prev => prev.filter(x => x.id !== id))
      setConfirmDelete(null)
    } catch (e) {
      console.error('Delete knowledge failed:', e)
      setErrorMsg('删除失败，请重试')
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <div className="flex h-full bg-gray-50 overflow-hidden">
      <TabBar value={view} onChange={onChangeView} />

      {/* 左侧：模块筛选 + 新建模块 */}
      <aside className="w-56 bg-white border-r border-gray-200 flex-shrink-0 flex flex-col">
        <div className="px-4 py-3 border-b border-gray-200">
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide">按模块筛选</div>
        </div>
        <div className="flex-1 overflow-y-auto py-1">
          {[
            { id: ALL, name: '全部模块', desc: null as string | null },
            // 仅在已有真实模块时才显示「项目级」过滤——否则它和「全部模块」结果完全相同
            ...(modules.length > 0
              ? [{ id: ORPHAN, name: '项目级（无模块）', desc: null as string | null }]
              : []),
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
                    ? 'bg-amber-50 text-amber-800 border-l-2 border-amber-500'
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
        {/* 新建模块入口 */}
        <div className="border-t border-gray-200 p-3">
          {moduleFormOpen ? (
            <div className="space-y-2">
              <input
                type="text"
                value={newModuleName}
                onChange={e => setNewModuleName(e.target.value)}
                placeholder="模块名称"
                disabled={creatingModule}
                className="w-full px-2 py-1 text-sm border border-gray-200 rounded focus:outline-none focus:ring-2 focus:ring-amber-200 focus:border-amber-400"
                onKeyDown={e => { if (e.key === 'Enter') void submitNewModule() }}
                autoFocus
              />
              <input
                type="text"
                value={newModuleDesc}
                onChange={e => setNewModuleDesc(e.target.value)}
                placeholder="描述（可选）"
                disabled={creatingModule}
                className="w-full px-2 py-1 text-xs border border-gray-200 rounded focus:outline-none focus:ring-2 focus:ring-amber-200 focus:border-amber-400"
                onKeyDown={e => { if (e.key === 'Enter') void submitNewModule() }}
              />
              <div className="flex gap-1">
                <button
                  type="button"
                  onClick={() => void submitNewModule()}
                  disabled={creatingModule || !newModuleName.trim()}
                  className="flex-1 inline-flex items-center justify-center gap-1 px-2 py-1 text-xs bg-amber-600 text-white rounded hover:bg-amber-700 disabled:opacity-50"
                >
                  {creatingModule ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                  创建
                </button>
                <button
                  type="button"
                  onClick={() => { setModuleFormOpen(false); setNewModuleName(''); setNewModuleDesc('') }}
                  disabled={creatingModule}
                  className="px-2 py-1 text-xs text-gray-500 border border-gray-200 rounded hover:bg-gray-50 disabled:opacity-50"
                >
                  取消
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setModuleFormOpen(true)}
              className="w-full inline-flex items-center justify-center gap-1 px-2 py-1.5 text-xs text-amber-700 border border-amber-300 border-dashed rounded hover:bg-amber-50 transition"
            >
              <Plus size={12} />
              新建模块
            </button>
          )}
        </div>
      </aside>

      {/* 主区 */}
      <main className="flex-1 flex flex-col overflow-hidden">
        <header className="flex items-center gap-3 px-6 py-3 border-b border-gray-200 bg-white">
          <BookOpen size={16} className="text-amber-600" />
          <h1 className="text-base font-semibold text-gray-800">知识库</h1>
          <span className="text-xs text-gray-400">沉淀的产品规则、约束、术语等</span>
          {activeRealModule && (
            <button
              type="button"
              onClick={openRelationDialog}
              className="ml-3 inline-flex items-center gap-1 px-2 py-1 text-xs text-purple-700 border border-purple-200 rounded hover:bg-purple-50 transition"
              title={`管理「${activeRealModule.name}」与其他模块的关联关系`}
            >
              <Network size={12} />
              管理关联关系
            </button>
          )}
          <button
            type="button"
            onClick={openSkillsDialog}
            className="ml-1 inline-flex items-center gap-1 px-2 py-1 text-xs text-indigo-700 border border-indigo-200 rounded hover:bg-indigo-50 transition"
            title={
              activeRealModule
                ? `查看与管理「${activeRealModule.name}」相关的测试设计经验（Skill）`
                : '查看与管理项目级测试设计经验（Skill）'
            }
          >
            <Brain size={12} />
            测试设计经验
          </button>
          <button
            type="button"
            onClick={() => setPromptDrawerOpen(true)}
            className="ml-1 inline-flex items-center gap-1 px-2 py-1 text-xs text-teal-700 border border-teal-200 rounded hover:bg-teal-50 transition"
            title="编辑、版本化与切换生效的系统提示词（按项目隔离）"
          >
            <FileCog size={12} />
            系统提示词
          </button>
          <div className="flex-1" />
          <form
            onSubmit={(e) => { e.preventDefault(); setQuery(searchInput.trim()) }}
            className="flex items-center gap-2"
          >
            <div className="relative">
              <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
              <input
                type="text"
                value={searchInput}
                onChange={e => setSearchInput(e.target.value)}
                placeholder="语义搜索（留空显示全部）…"
                className="w-72 pl-8 pr-8 py-1.5 text-sm border border-gray-200 rounded-md focus:outline-none focus:ring-2 focus:ring-amber-200 focus:border-amber-400"
              />
              {searchInput && (
                <button
                  type="button"
                  onClick={() => { setSearchInput(''); setQuery('') }}
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                  title="清空"
                >
                  <X size={12} />
                </button>
              )}
            </div>
            <button
              type="submit"
              className="px-3 py-1.5 text-sm bg-amber-600 text-white rounded-md hover:bg-amber-700 transition"
            >
              搜索
            </button>
            <button
              type="button"
              onClick={() => void reload()}
              className="p-1.5 text-gray-500 hover:text-gray-800 border border-gray-200 rounded-md"
              title="刷新"
            >
              <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            </button>
          </form>
        </header>

        {stats && (
          <div className="px-6 py-3 border-b border-gray-200 bg-gradient-to-r from-amber-50/60 to-orange-50/40">
            <div className="flex items-start gap-4 flex-wrap">
              <div className="flex items-center gap-2 text-xs font-semibold text-amber-800/90">
                <BarChart3 size={14} />
                总览
              </div>
              <div className="flex flex-wrap gap-3">
                <StatChip
                  icon={<BookOpen size={12} />}
                  label="总条目"
                  value={stats.total}
                />
                <StatChip
                  icon={<Sparkles size={12} />}
                  label={`近 ${stats.recent_days} 天新增`}
                  value={stats.recent_added}
                  highlight={stats.recent_added > 0}
                />
                <StatChip
                  icon={<FileText size={12} />}
                  label="已上传文档"
                  value={stats.documents.total}
                  hint={
                    stats.documents.with_pending_drafts > 0
                      ? `（${stats.documents.with_pending_drafts} 份草稿待审核）`
                      : undefined
                  }
                />
                <StatChip
                  icon={<Network size={12} />}
                  label="模块覆盖"
                  value={`${stats.module_coverage.modules_with_knowledge}/${stats.module_coverage.modules_total}`}
                />
              </div>
              <div className="flex-1" />
              <button
                type="button"
                onClick={() => void reloadStats()}
                className="p-1 text-gray-400 hover:text-gray-700"
                title="刷新统计"
              >
                <RefreshCw size={12} className={statsLoading ? 'animate-spin' : ''} />
              </button>
            </div>
            {stats.by_type.length > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <span className="text-[11px] text-amber-800/70">类型分布：</span>
                {stats.by_type.map(t => (
                  <span
                    key={t.knowledge_type}
                    className={`px-1.5 py-0.5 text-[11px] rounded border ${TYPE_BADGE[t.knowledge_type] || 'bg-gray-100 text-gray-700 border-gray-200'}`}
                  >
                    {TYPE_LABEL[t.knowledge_type] || t.knowledge_type} · {t.count}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}

        {(recentFeedbackLoading || recentFeedback.length > 0) && (
          <div className="px-6 py-2 border-b border-gray-200 bg-indigo-50/30">
            <div className="flex items-center gap-2 text-xs font-semibold text-indigo-800/90 mb-1.5">
              <History size={12} />
              最近修改沉淀
              <span className="text-[11px] font-normal text-indigo-700/60">
                {activeRealModule ? `（仅显示「${activeRealModule.name}」）` : '（全部模块）'}
              </span>
              {recentFeedbackLoading && <Loader2 size={12} className="animate-spin text-indigo-400" />}
            </div>
            {recentFeedback.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {recentFeedback.map(fb => (
                  <div
                    key={fb.id}
                    className="inline-flex items-start gap-1.5 max-w-md px-2 py-1 bg-white border border-indigo-100 rounded text-[11px]"
                    title={fb.summary || undefined}
                  >
                    {fb.intent && (
                      <span className="px-1 py-0.5 bg-indigo-100 text-indigo-700 border border-indigo-200 rounded text-[10px] whitespace-nowrap">
                        {fb.intent}
                      </span>
                    )}
                    <span className="text-gray-700 truncate">
                      {fb.test_case_name}
                    </span>
                    {fb.extracted_rule_count > 0 && (
                      <span className="text-emerald-600 whitespace-nowrap" title="本次修改沉淀的规则条数">
                        +{fb.extracted_rule_count} 条规则
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-2">
          {errorMsg && (
            <div className="px-3 py-2 rounded bg-red-50 border border-red-200 text-sm text-red-700">
              {errorMsg}
            </div>
          )}

          {activeRealModule && (
            <ModuleDetailCard
              key={activeRealModule.id}
              module={activeRealModule}
              allModules={modules}
              onModuleUpdated={(m) => setModules(prev => prev.map(x => x.id === m.id ? m : x))}
              onModuleDeleted={(id) => {
                setModules(prev => prev.filter(x => x.id !== id))
                setActiveModule(ALL)
              }}
              onManageSkills={openSkillsDialog}
              onEditSkill={(s) => { setSkillsDialogOpen(true); void reloadSkills(); void startEditSkill(s) }}
            />
          )}

          {loading && items.length === 0 && (
            <div className="flex items-center justify-center py-16 text-sm text-gray-400">
              <Loader2 size={14} className="animate-spin mr-2" />
              加载中…
            </div>
          )}

          {!loading && items.length === 0 && (
            <div className="flex flex-col items-center justify-center py-16 text-sm text-gray-400">
              <BookOpen size={32} className="opacity-30 mb-3" />
              <div>当前筛选条件下没有知识条目</div>
              {query && <div className="text-xs mt-1">尝试清空搜索词或切换"全部模块"</div>}
            </div>
          )}

          {items.map(item => {
            const isEditing = editingId === item.id
            const typeLabel = TYPE_LABEL[item.knowledge_type] || item.knowledge_type
            const typeBadge = TYPE_BADGE[item.knowledge_type] || 'bg-gray-100 text-gray-700 border-gray-200'
            const moduleName = item.module_id == null ? '项目级' : (moduleNameById.get(item.module_id) || `#${item.module_id}`)
            return (
              <div
                key={item.id}
                className="bg-white border border-gray-200 rounded-lg px-4 py-3 hover:border-amber-200 transition"
              >
                <div className="flex items-start gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex flex-wrap items-center gap-2 text-xs mb-1.5">
                      <span className={`px-1.5 py-0.5 rounded border font-medium ${typeBadge}`}>
                        {typeLabel}
                      </span>
                      <span className="text-gray-500">模块：{moduleName}</span>
                      <span className="text-gray-400">来源：{item.source}</span>
                      <span className="text-gray-400">v{item.version}</span>
                      <span className="text-gray-400">{formatDate(item.created_at)}</span>
                      {item.distance != null && (
                        <span className="text-gray-400" title="余弦距离，越小越相关">
                          相关度 {(1 - item.distance).toFixed(2)}
                        </span>
                      )}
                    </div>

                    {isEditing && editDraft ? (
                      <div className="space-y-2">
                        <textarea
                          value={editDraft.content}
                          onChange={e => setEditDraft(d => d ? { ...d, content: e.target.value } : d)}
                          rows={Math.min(8, Math.max(3, editDraft.content.split('\n').length + 1))}
                          className="w-full px-3 py-2 text-sm border border-amber-300 rounded focus:outline-none focus:ring-2 focus:ring-amber-200"
                        />
                        <div className="flex items-center gap-3">
                          <label className="text-xs text-gray-600">置信度</label>
                          <input
                            type="range"
                            min={0.1}
                            max={0.95}
                            step={0.05}
                            value={editDraft.confidence}
                            onChange={e => setEditDraft(d => d ? { ...d, confidence: parseFloat(e.target.value) } : d)}
                            className="flex-1 max-w-xs accent-amber-600"
                          />
                          <span className="text-xs font-mono text-amber-700 w-12 text-right">
                            {(editDraft.confidence * 100).toFixed(0)}%
                          </span>
                        </div>
                      </div>
                    ) : (
                      <div className="text-sm text-gray-800 whitespace-pre-wrap break-words leading-relaxed">
                        {item.content}
                      </div>
                    )}

                    {!isEditing && (
                      <div className="mt-1 text-xs text-gray-400">
                        置信度 {(item.confidence * 100).toFixed(0)}%
                      </div>
                    )}
                  </div>

                  <div className="flex items-center gap-1 flex-shrink-0">
                    {isEditing ? (
                      <>
                        <button
                          type="button"
                          onClick={() => void saveEdit(item.id)}
                          disabled={savingId === item.id}
                          title="保存"
                          className="p-1.5 text-emerald-600 hover:bg-emerald-50 rounded disabled:opacity-50"
                        >
                          {savingId === item.id ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
                        </button>
                        <button
                          type="button"
                          onClick={cancelEdit}
                          disabled={savingId === item.id}
                          title="取消"
                          className="p-1.5 text-gray-500 hover:bg-gray-100 rounded disabled:opacity-50"
                        >
                          <X size={14} />
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          type="button"
                          onClick={() => startEdit(item)}
                          title="编辑"
                          className="p-1.5 text-gray-500 hover:text-amber-700 hover:bg-amber-50 rounded"
                        >
                          <Edit2 size={14} />
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirmDelete(item)}
                          title="删除"
                          className="p-1.5 text-gray-500 hover:text-red-600 hover:bg-red-50 rounded"
                        >
                          <Trash2 size={14} />
                        </button>
                      </>
                    )}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      </main>

      {/* 删除确认弹窗 */}
      {confirmDelete && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl p-5 w-96 max-w-[90vw]">
            <div className="flex items-center gap-2 mb-3 text-red-600">
              <Trash2 size={16} />
              <h3 className="text-base font-semibold">确认删除该条知识？</h3>
            </div>
            <div className="text-sm text-gray-600 mb-1">
              {TYPE_LABEL[confirmDelete.knowledge_type] || confirmDelete.knowledge_type}
            </div>
            <div className="text-sm text-gray-800 bg-gray-50 border border-gray-200 rounded px-3 py-2 mb-4 max-h-40 overflow-auto whitespace-pre-wrap break-words">
              {confirmDelete.content}
            </div>
            <div className="text-xs text-gray-500 mb-4">
              删除后该条目不会再参与未来用例生成的检索召回，操作不可恢复。
            </div>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmDelete(null)}
                disabled={deletingId === confirmDelete.id}
                className="px-3 py-1.5 text-sm border border-gray-200 rounded hover:bg-gray-50 disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => void doDelete(confirmDelete.id)}
                disabled={deletingId === confirmDelete.id}
                className="inline-flex items-center gap-1 px-3 py-1.5 text-sm bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50"
              >
                {deletingId === confirmDelete.id && <Loader2 size={12} className="animate-spin" />}
                确认删除
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 模块关联关系管理弹窗 */}
      {relationDialogOpen && activeRealModule && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl p-5 w-[600px] max-w-[92vw] max-h-[80vh] flex flex-col">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2 text-purple-700">
                <Network size={16} />
                <h3 className="text-base font-semibold">
                  「{activeRealModule.name}」的模块关联关系
                </h3>
              </div>
              <button
                type="button"
                onClick={() => setRelationDialogOpen(false)}
                className="p-1 text-gray-400 hover:text-gray-700 rounded"
              >
                <X size={16} />
              </button>
            </div>

            <div className="text-xs text-gray-500 mb-3">
              用例生成时会自动把该模块的关联关系注入 prompt，让 LLM 在跨模块联动场景中考虑上下游影响。
            </div>

            {/* 已有关系列表 */}
            <div className="flex-1 overflow-y-auto border border-gray-200 rounded mb-3">
              {relationsLoading ? (
                <div className="flex items-center justify-center py-6 text-sm text-gray-400">
                  <Loader2 size={14} className="animate-spin mr-2" />
                  加载中…
                </div>
              ) : relations.length === 0 ? (
                <div className="text-center py-6 text-sm text-gray-400">
                  暂无关联关系。下方添加一条 →
                </div>
              ) : (
                <ul className="divide-y divide-gray-100">
                  {relations.map(r => {
                    const isSource = r.source_module_id === activeRealModule.id
                    const otherName = isSource ? r.target_module_name : r.source_module_name
                    const arrow = isSource ? '→' : '←'
                    return (
                      <li key={r.id} className="px-3 py-2 flex items-center gap-2 text-sm">
                        <span className="font-medium text-gray-800">{activeRealModule.name}</span>
                        <span className="px-1.5 py-0.5 text-xs rounded bg-purple-100 text-purple-700 border border-purple-200">
                          {RELATION_LABEL[r.relation_type] || r.relation_type}
                        </span>
                        <span className="text-gray-400">{arrow}</span>
                        <span className="font-medium text-gray-800 truncate">{otherName || `#${isSource ? r.target_module_id : r.source_module_id}`}</span>
                        {r.description && (
                          <span className="text-xs text-gray-500 truncate flex-1">— {r.description}</span>
                        )}
                        <button
                          type="button"
                          onClick={() => void removeRelation(r.id)}
                          title="删除该关系"
                          className="ml-auto p-1 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded"
                        >
                          <Trash2 size={12} />
                        </button>
                      </li>
                    )
                  })}
                </ul>
              )}
            </div>

            {/* 新建表单 */}
            <div className="border border-purple-200 rounded p-3 bg-purple-50/40 space-y-2">
              <div className="text-xs font-semibold text-purple-700">新建关联关系</div>
              <div className="flex items-center gap-2 text-sm">
                <span className="px-2 py-1 bg-white border border-gray-200 rounded text-gray-700 text-xs whitespace-nowrap">
                  {activeRealModule.name}
                </span>
                <select
                  value={newRelationType}
                  onChange={e => setNewRelationType(e.target.value as ModuleRelationType)}
                  disabled={submittingRelation}
                  className="px-2 py-1 text-xs border border-gray-200 rounded bg-white focus:outline-none focus:ring-1 focus:ring-purple-300"
                >
                  {RELATION_TYPES.map(t => (
                    <option key={t} value={t}>{RELATION_LABEL[t]}</option>
                  ))}
                </select>
                <select
                  value={newRelationTarget}
                  onChange={e => setNewRelationTarget(e.target.value === '' ? '' : Number(e.target.value))}
                  disabled={submittingRelation}
                  className="flex-1 px-2 py-1 text-xs border border-gray-200 rounded bg-white focus:outline-none focus:ring-1 focus:ring-purple-300"
                >
                  <option value="">选择目标模块…</option>
                  {modules.filter(m => m.id !== activeRealModule.id).map(m => (
                    <option key={m.id} value={m.id}>{m.name}</option>
                  ))}
                </select>
              </div>
              <input
                type="text"
                value={newRelationDesc}
                onChange={e => setNewRelationDesc(e.target.value)}
                placeholder="描述（可选）。例：下单成功后触发库存扣减"
                disabled={submittingRelation}
                className="w-full px-2 py-1 text-xs border border-gray-200 rounded focus:outline-none focus:ring-1 focus:ring-purple-300"
              />
              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={() => void submitNewRelation()}
                  disabled={submittingRelation || !newRelationTarget}
                  className="inline-flex items-center gap-1 px-3 py-1 text-xs bg-purple-600 text-white rounded hover:bg-purple-700 disabled:opacity-50"
                >
                  {submittingRelation ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />}
                  添加
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Skills 抽屉 —— 测试设计经验管理 */}
      {skillsDialogOpen && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl p-5 w-[680px] max-w-[92vw] max-h-[85vh] flex flex-col">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2 text-indigo-700">
                <Brain size={16} />
                <h3 className="text-base font-semibold">
                  测试设计经验
                  {activeRealModule
                    ? <span className="text-gray-500 font-normal">（{activeRealModule.name}）</span>
                    : <span className="text-gray-500 font-normal">（项目级）</span>}
                </h3>
              </div>
              <button
                type="button"
                onClick={() => { setSkillsDialogOpen(false); setSkillForm(null); setRegenMessage(null) }}
                className="p-1 text-gray-400 hover:text-gray-700 rounded"
              >
                <X size={16} />
              </button>
            </div>

            <div className="text-xs text-gray-500 mb-3">
              Skill 是从该模块历史用例修改中沉淀的「测试设计要点」（Markdown），
              用例生成时会作为最高优先级的提示词上下文注入。
            </div>

            {/* 操作按钮 */}
            <div className="flex items-center gap-2 mb-3 flex-wrap">
              <button
                type="button"
                onClick={startNewSkill}
                className="inline-flex items-center gap-1 px-2 py-1 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-700"
              >
                <Plus size={12} />
                新建 Skill
              </button>
              <button
                type="button"
                onClick={() => void triggerRegenerate()}
                disabled={!activeRealModule || regenLoading}
                title={
                  activeRealModule
                    ? '基于该模块的最近反馈与知识自动归纳一条 Skill'
                    : '请先在左侧选择一个具体模块再触发自动归纳'
                }
                className="inline-flex items-center gap-1 px-2 py-1 text-xs text-indigo-700 border border-indigo-300 rounded hover:bg-indigo-50 disabled:opacity-50"
              >
                {regenLoading
                  ? <Loader2 size={12} className="animate-spin" />
                  : <Wand2 size={12} />}
                自动归纳
              </button>
              <button
                type="button"
                onClick={() => void reloadSkills()}
                className="p-1 text-gray-400 hover:text-gray-700"
                title="刷新"
              >
                <RefreshCw size={12} className={skillsLoading ? 'animate-spin' : ''} />
              </button>
              {regenMessage && (
                <div className="ml-2 text-xs text-indigo-700 truncate flex-1" title={regenMessage}>
                  {regenMessage}
                </div>
              )}
            </div>

            {/* 编辑/新建表单 */}
            {skillForm && (
              <div className="border border-indigo-200 rounded p-3 bg-indigo-50/40 space-y-2 mb-3">
                <div className="text-xs font-semibold text-indigo-700">
                  {skillForm.id ? `编辑 Skill #${skillForm.id}` : '新建 Skill'}
                  {!skillForm.id && (
                    <span className="ml-2 text-gray-500 font-normal">
                      绑定到：{activeRealModule ? activeRealModule.name : '项目级'}
                    </span>
                  )}
                </div>
                <input
                  type="text"
                  value={skillForm.name}
                  onChange={e => setSkillForm(f => f ? { ...f, name: e.target.value } : f)}
                  placeholder="Skill 名称（例：登录模块测试要点）"
                  disabled={savingSkill}
                  className="w-full px-2 py-1 text-sm border border-gray-200 rounded focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-400"
                />
                <textarea
                  value={skillForm.content}
                  onChange={e => setSkillForm(f => f ? { ...f, content: e.target.value } : f)}
                  rows={10}
                  placeholder={'Markdown 格式，例：\n## 设计要点\n- 关注密码长度边界（8-16）\n## 易错场景\n- 锁定后倒计时未刷新'}
                  disabled={savingSkill}
                  className="w-full px-2 py-2 text-xs font-mono border border-gray-200 rounded focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-400"
                />
                <div className="flex justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => setSkillForm(null)}
                    disabled={savingSkill}
                    className="px-3 py-1 text-xs text-gray-500 border border-gray-200 rounded hover:bg-gray-50 disabled:opacity-50"
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    onClick={() => void submitSkillForm()}
                    disabled={savingSkill || !skillForm.name.trim() || !skillForm.content.trim()}
                    className="inline-flex items-center gap-1 px-3 py-1 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
                  >
                    {savingSkill ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                    保存
                  </button>
                </div>
              </div>
            )}

            {/* Skill 列表 */}
            <div className="flex-1 overflow-y-auto border border-gray-200 rounded">
              {skillsLoading && visibleSkills.length === 0 ? (
                <div className="flex items-center justify-center py-8 text-sm text-gray-400">
                  <Loader2 size={14} className="animate-spin mr-2" />
                  加载中…
                </div>
              ) : visibleSkills.length === 0 ? (
                <div className="text-center py-8 text-sm text-gray-400">
                  当前模块暂无 Skill。可点击「新建 Skill」手写一条，或选定模块后点「自动归纳」。
                </div>
              ) : (
                <ul className="divide-y divide-gray-100">
                  {visibleSkills.map(s => {
                    const moduleLabel = s.module_id == null
                      ? '项目级'
                      : (moduleNameById.get(s.module_id) || `#${s.module_id}`)
                    const isAuto = s.source === 'auto_generated'
                    return (
                      <li key={s.id} className="px-3 py-2.5 hover:bg-gray-50">
                        <div className="flex items-start gap-2">
                          <div className="flex-1 min-w-0">
                            <div className="flex flex-wrap items-center gap-2 text-xs mb-0.5">
                              <span className="font-medium text-gray-800 truncate">{s.name}</span>
                              <span
                                className={`px-1 py-0.5 rounded border text-[10px] ${
                                  isAuto
                                    ? 'bg-purple-100 text-purple-700 border-purple-200'
                                    : 'bg-gray-100 text-gray-700 border-gray-200'
                                }`}
                              >
                                {isAuto ? '自动归纳' : '人工'}
                              </span>
                              <span className="text-gray-400">模块：{moduleLabel}</span>
                              <span className="text-gray-400">v{s.version}</span>
                              <span className="text-gray-400">{formatDate(s.updated_at || s.created_at)}</span>
                            </div>
                          </div>
                          <div className="flex items-center gap-1 flex-shrink-0">
                            <button
                              type="button"
                              onClick={() => void startEditSkill(s)}
                              title="查看 / 编辑"
                              className="p-1.5 text-gray-500 hover:text-indigo-700 hover:bg-indigo-50 rounded"
                            >
                              <Edit2 size={12} />
                            </button>
                            <button
                              type="button"
                              onClick={() => void removeSkill(s.id)}
                              disabled={deletingSkillId === s.id}
                              title="删除"
                              className="p-1.5 text-gray-500 hover:text-red-600 hover:bg-red-50 rounded disabled:opacity-50"
                            >
                              {deletingSkillId === s.id
                                ? <Loader2 size={12} className="animate-spin" />
                                : <Trash2 size={12} />}
                            </button>
                          </div>
                        </div>
                      </li>
                    )
                  })}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}

      <PromptManagerDrawer open={promptDrawerOpen} onClose={() => setPromptDrawerOpen(false)} />
    </div>
  )
}


function StatChip({
  icon, label, value, hint, highlight,
}: {
  icon: React.ReactNode
  label: string
  value: number | string
  hint?: string
  highlight?: boolean
}) {
  return (
    <div
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-xs ${
        highlight
          ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
          : 'bg-white border-gray-200 text-gray-700'
      }`}
    >
      <span className={highlight ? 'text-emerald-600' : 'text-gray-400'}>{icon}</span>
      <span className="text-[11px] text-gray-500">{label}</span>
      <span className="font-semibold">{value}</span>
      {hint && <span className="text-[10px] text-gray-400">{hint}</span>}
    </div>
  )
}
