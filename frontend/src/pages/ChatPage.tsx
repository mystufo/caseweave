import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import type {
  ChatSession, ChatMessage as IChatMessage, TestCase, UploadResult,
  ClarificationQuestion, ClarificationRoundHistory, ClarificationStateDTO,
  KnowledgeHit, KnowledgeDraft, ModuleSummary,
} from '../api/client'
import {
  fetchSessions, fetchMessages, createSession, renameSession, deleteSession,
  generateCases, generateMindmap, fetchSessionCases, fetchClarificationState, fetchKnowledgePreview,
  streamChat, streamUpload, streamLarkImport, streamLarkMindmapImport, streamFollowupClarification,
  streamInitialClarification, streamMindmapUpload, streamPipelineStart,
  confirmPendingKnowledge, extractCombinedDrafts,
  createModule, updateDocumentModule, fetchModules,
  exportSessionUrl,
} from '../api/client'
import SessionList from '../components/SessionList'
import ChatMessage from '../components/ChatMessage'
import MessageInput from '../components/MessageInput'
import ClarificationPanel from '../components/ClarificationPanel'
import KnowledgePreviewPanel from '../components/KnowledgePreviewPanel'
import KnowledgeDraftReviewPanel from '../components/KnowledgeDraftReviewPanel'
import ModuleConfirmPanel from '../components/ModuleConfirmPanel'
import FlowSteps from '../components/FlowSteps'
import TestCaseTable from '../components/TestCaseTable'
import LarkUrlDialog, { type LarkUrlSubmit } from '../components/LarkUrlDialog'
import MindmapPasteDialog from '../components/MindmapPasteDialog'
import StopConfirmDialog from '../components/StopConfirmDialog'
import TabBar, { type ViewKey } from '../components/TabBar'
import { FileText, Loader2, Square, Brain, ExternalLink, Upload, Network, Pencil, Link as LinkIcon } from 'lucide-react'

interface PageProps {
  view: ViewKey
  onChangeView: (v: ViewKey) => void
}

const MAX_ROUNDS = 5

// 文档正文喂给 LLM 前的截断阈值（字符数），与后端 doc_parser.DEFAULT_DOC_LIMIT 保持一致。
// 超过则 chip 显示"已截断"提示。
const DOC_PREVIEW_LIMIT = 30000

// ── Per-session state ────────────────────────────────────────────────────────
// 每个会话有一份独立的运行态。切换会话只是换"视角"，不取消任何在跑的任务。
// 流式回调通过启动时捕获的 sid 写回对应 slot，所以切走再切回来能看到最新结果。
interface SessionState {
  // 会话用途（对话页顶层选定，固定不变）：cases=生成用例；mindmap=从需求文档生成测试脑图。
  mode: 'cases' | 'mindmap'
  messages: IChatMessage[]
  testCases: TestCase[]
  loaded: boolean  // messages + testCases 是否已从后端拉过

  // chat streaming
  streaming: boolean
  streamBuffer: string

  // upload
  uploading: boolean
  uploadStage: string | null
  uploadProgress: string

  // 「开始生成」闸门：上传只暂存文档，用户点「开始生成」后跑模块分类 + 知识检索预览。
  // 期间显示按钮 loading，完成后进入模块确认卡 / 知识审核 / 澄清流程。
  pipelineStarting: boolean

  // clarification
  uploadResult: UploadResult | null
  // 脑图独立的上传 slot —— 与 uploadResult（PRD）并存，至少其一非 null 才能进入澄清。
  uploadMindmap: {
    documentId: number
    filename: string
    stats: { chunks: number; tables: number; raw_text_length: number }
  } | null
  confirmedModuleName: string | null
  // 上一步「模块确认卡」拍板的模块 id（null=无模块）。草稿审核面板据此默认选中同一模块，
  // 直接用 id 而非名字反查——避免新建模块时 modules 列表异步刷新还没到导致匹配失败。
  confirmedModuleId: number | null
  confirmedCasePrefix: string | null
  clarificationRounds: ClarificationRoundHistory[]
  currentQuestions: ClarificationQuestion[] | null
  currentSummary: string
  currentRound: number
  followupActive: boolean
  followupBuffer: string

  // generation
  generating: boolean
  // 从 PRD 生成测试脑图并写入飞书：生成中标志 + 结果链接（仅本会话可见，随 taskMap 隔离）
  mindmapGenerating: boolean
  mindmapResult: { url: string; title: string } | null
  // 脑图模式专用：澄清是否已完成、可以生成脑图了。初轮/追问返回 ready_to_generate 或
  // 问题为空、或达 MAX_ROUNDS 时置 true —— 露出「生成测试脑图」卡的门控。
  mindmapReadyToGenerate: boolean
  // 知识库确认阶段。两个时机都会用到这个面板：
  //   phase: 'clarify'  → 文档刚 persist 完，跑 Clarifier 之前；onConfirm 调 streamInitialClarification
  //   phase: 'generate' → 澄清完成，跑 Generator 之前；onConfirm 调 runGenerate
  // 用 phase 区分是为了让一个组件复用、按钮文案/确认行为不同。
  knowledgePreview: {
    phase: 'clarify'
    documentId: number | null   // PRD doc id；脑图独立模式时为 null
    mindmapDocumentId: number | null
    moduleName: string | null   // clarify 阶段还没确认模块名，可能为空
    casePrefix: string | null
    loading: boolean
    hits: KnowledgeHit[]
  } | {
    phase: 'generate'
    documentId: number | null
    mindmapDocumentId: number | null
    moduleName: string
    casePrefix: string
    rounds: ClarificationRoundHistory[]
    loading: boolean
    hits: KnowledgeHit[]
  } | null
  // 后端把 state.status 标成 "generating"（澄清完成、待生成）但本会话用例仍为空——
  // 通常是上一次点了「生成」但请求被关屏/掉网中断了。带上这个字段让 UI 显示「继续生成」按钮。
  pendingGenerate: {
    documentId: number | null
    mindmapDocumentId: number | null
    moduleName: string
    casePrefix: string
    rounds: ClarificationRoundHistory[]
  } | null

  // 上传后由 LLM 抽取出的"产品知识草稿"——用户审核后才决定哪些条目写入 knowledge_entries。
  // 两个 slot 与 uploadResult / uploadMindmap 平行；任一未 settle（!= null）就阻塞澄清入口。
  // settled 字段表示是否调过 confirm 接口（避免幂等重复）；后端清空 pending_knowledge 后我们就把 slot 置 null。
  prdDraftReview: {
    documentId: number
    role: 'prd'
    filename: string | null
    moduleName: string | null
    drafts: KnowledgeDraft[]
    submitting: boolean
  } | null
  mindmapDraftReview: {
    documentId: number
    role: 'mindmap'
    filename: string | null
    moduleName: string | null
    drafts: KnowledgeDraft[]
    submitting: boolean
  } | null

  // 上传完成后正在跑「PRD + 脑图」合并知识抽取（脑图优先）。抽取期间显示 loader，
  // 抽完把草稿落到 prdDraftReview / mindmapDraftReview 其一。
  extractingDrafts: boolean

  // LLM 在上传时对文档归属模块的判定 → 一律弹「模块确认卡」让用户拍板（即使高置信自动命中也要确认）。
  // 用户可在下拉里改选其它已有模块 / 不归入模块（项目级）/ 新建模块；确认后把文档归入所选模块。
  // 处理完（确认或忽略）才解锁下方知识草稿审核与开始澄清。
  moduleDecision: {
    documentId: number | null
    // LLM 命中的既有模块（自动落库或中置信建议），用于文案与默认选中
    suggestedModuleId: number | null
    suggestedModuleName: string | null
    applied: boolean          // 后端是否已高置信自动落库（仅影响文案）
    confidence: number
    reasoning: string
    // 当前下拉选中的模块 id（null = 不归入模块 / 项目级）
    selectedModuleId: number | null
    // 选择「新建模块」时展开的可编辑字段
    createNew: boolean
    createName: string
    createCode: string
    createDescription: string | null
    creating: boolean
  } | null
}

const emptyState = (mode: 'cases' | 'mindmap' = 'cases'): SessionState => ({
  mode,
  messages: [],
  testCases: [],
  loaded: false,
  streaming: false,
  streamBuffer: '',
  uploading: false,
  uploadStage: null,
  uploadProgress: '',
  pipelineStarting: false,
  uploadResult: null,
  uploadMindmap: null,
  confirmedModuleName: null,
  confirmedModuleId: null,
  confirmedCasePrefix: null,
  clarificationRounds: [],
  currentQuestions: null,
  currentSummary: '',
  currentRound: 1,
  followupActive: false,
  followupBuffer: '',
  generating: false,
  mindmapGenerating: false,
  mindmapResult: null,
  mindmapReadyToGenerate: false,
  knowledgePreview: null,
  pendingGenerate: null,
  prdDraftReview: null,
  mindmapDraftReview: null,
  extractingDrafts: false,
  moduleDecision: null,
})

const isBusy = (s: SessionState) =>
  s.streaming || s.uploading || s.followupActive || s.generating
  || s.pipelineStarting
  || s.extractingDrafts  || (s.knowledgePreview?.loading ?? false)
  || (s.prdDraftReview?.submitting ?? false)
  || (s.mindmapDraftReview?.submitting ?? false)

// 从 SessionState 推导流程步骤条的「当前步」索引（0-based）。返回值 ≥ 步数表示全部完成。
// 只读现有字段，不新增状态。
const deriveFlowStep = (s: SessionState): number => {
  if (s.mode === 'mindmap') {
    // 脑图：0 上传需求文档 / 1 澄清 / 2 生成脑图 / 3 存入飞书
    if (s.mindmapResult) return 4
    if (s.mindmapReadyToGenerate) return 2
    if (s.uploadResult?.document_id != null) return 1
    return 0
  }
  // 用例：0 上传资料 / 1 确认模块 / 2 审核知识 / 3 澄清 / 4 生成用例
  if (s.testCases.length > 0) return 5
  if (s.generating || s.pendingGenerate) return 4
  if (s.knowledgePreview?.phase === 'generate'
      || (s.currentQuestions && s.currentQuestions.length > 0)
      || s.followupActive) return 3
  if (s.prdDraftReview || s.mindmapDraftReview) return 2
  if (s.moduleDecision) return 1
  if (s.uploadResult || s.uploadMindmap) return 1  // 上传完成，等下一步
  return 0
}

// 把后端返回的澄清运行态映射回 SessionState 部分字段。
// uploadResult 是面板渲染的"哨兵字段"——伪造一个最小 shape，filename/stats 此时已无渲染消费方。
// hasCases: 该会话已经有用例了吗？决定 status="generating" 时该不该显示「继续生成」按钮。
//
// 关于 awaiting_clarification：B 方案下文档 persist 后停在知识预览阶段，刷新回来后
// 这里只设置 uploadResult（让后续渲染 KnowledgePreviewPanel）+ 一个临时 knowledgePreview
// loading 状态——真正的 hits 由调用方去 fetchKnowledgePreview 异步补全。
function stateFromDTO(dto: ClarificationStateDTO, hasCases: boolean): Partial<SessionState> {
  if (dto.document_id == null && dto.mindmap_document_id == null) return {}
  // 重建 PRD upload slot（如有）
  let uploadResult: UploadResult | null = null
  if (dto.document_id != null) {
    uploadResult = {
      document_id: dto.document_id,
      filename: dto.prd_filename || '',
      stats: dto.prd_stats || { chunks: 0, tables: 0, raw_text_length: 0 },
      clarification: {
        summary: dto.summary || '',
        module_detected: dto.module_detected || '',
        case_prefix_suggestion: dto.case_prefix_suggestion || undefined,
        questions: dto.current_questions,
        ready_to_generate: dto.ready_to_generate,
      },
    }
  }
  // 重建脑图 upload slot（如有）
  let uploadMindmap: SessionState['uploadMindmap'] = null
  if (dto.mindmap_document_id != null) {
    uploadMindmap = {
      documentId: dto.mindmap_document_id,
      filename: dto.mindmap_filename || '',
      stats: dto.mindmap_stats || { chunks: 0, tables: 0, raw_text_length: 0 },
    }
  }

  // status === 'generating' 且本会话没用例 → 上次的 generate 请求中断了，给个「继续生成」按钮；
  //                      已经有用例了 → 单纯是 done，什么都不渲染。
  // status === 'done' → 用例齐了，面板归零。
  // status === 'awaiting_answers' → 还在澄清中，渲染面板。
  // status === 'awaiting_clarification' → 知识预览阶段（B 方案）；先给个 loading 占位，调用方会补 hits。
  let pending: SessionState['pendingGenerate'] = null
  if (
    dto.status === 'generating'
    && !hasCases
    && dto.confirmed_module_name
    && dto.confirmed_case_prefix
    && (dto.document_id != null || dto.mindmap_document_id != null)
  ) {
    pending = {
      documentId: dto.document_id,
      mindmapDocumentId: dto.mindmap_document_id,
      moduleName: dto.confirmed_module_name,
      casePrefix: dto.confirmed_case_prefix,
      rounds: dto.rounds,
    }
  }

  let knowledgePreview: SessionState['knowledgePreview'] = null
  if (dto.status === 'awaiting_clarification') {
    knowledgePreview = {
      phase: 'clarify',
      documentId: dto.document_id,
      mindmapDocumentId: dto.mindmap_document_id,
      moduleName: dto.module_detected || null,
      casePrefix: dto.case_prefix_suggestion || null,
      loading: true,
      hits: [],
    }
  }

  // 还没确认入库的草稿——刷新后回到审核面板，让用户在跨会话切换/掉网后能继续
  const prdDraftReview: SessionState['prdDraftReview'] =
    dto.document_id != null && Array.isArray(dto.prd_pending_drafts) && dto.prd_pending_drafts.length > 0
      ? {
          documentId: dto.document_id,
          role: 'prd',
          filename: dto.prd_filename,
          moduleName: dto.module_detected,
          drafts: dto.prd_pending_drafts,
          submitting: false,
        }
      : null
  const mindmapDraftReview: SessionState['mindmapDraftReview'] =
    dto.mindmap_document_id != null && Array.isArray(dto.mindmap_pending_drafts) && dto.mindmap_pending_drafts.length > 0
      ? {
          documentId: dto.mindmap_document_id,
          role: 'mindmap',
          filename: dto.mindmap_filename,
          moduleName: dto.module_detected,
          drafts: dto.mindmap_pending_drafts,
          submitting: false,
        }
      : null

  // 模块确认卡——刷新后由后端从已落库的分类气泡复原（见 clarification_state 路由）。
  // 后端只复原"建议新建模块"这一种（applied=false 且文档仍未归类）；高置信自动命中的
  // 确认卡是实时 SSE 流内的一次性交互，刷新后若文档已归类则不再复原（属可接受行为）。
  const moduleDecision: SessionState['moduleDecision'] =
    dto.module_proposal && dto.module_proposal.name
      ? {
          documentId: dto.module_proposal.document_id,
          suggestedModuleId: null,
          suggestedModuleName: null,
          applied: false,
          confidence: 0,
          reasoning: '',
          selectedModuleId: null,
          createNew: true,
          createName: dto.module_proposal.name,
          createCode: dto.module_proposal.code,
          createDescription: dto.module_proposal.description,
          creating: false,
        }
      : null

  return {
    uploadResult,
    uploadMindmap,
    confirmedModuleName: dto.confirmed_module_name,
    confirmedCasePrefix: dto.confirmed_case_prefix,
    clarificationRounds: dto.rounds,
    currentQuestions: dto.status === 'awaiting_answers' ? dto.current_questions : null,
    currentSummary: dto.summary || '',
    currentRound: dto.current_round,
    // 脑图模式刷新恢复：澄清已完成（非"待答问题"/"知识预览"）即视为就绪，露出生成卡。
    // 该字段仅被脑图模式 JSX 消费，对用例模式无副作用。
    mindmapReadyToGenerate:
      dto.document_id != null
      && dto.status !== 'awaiting_answers'
      && dto.status !== 'awaiting_clarification',
    pendingGenerate: pending,
    knowledgePreview,
    prdDraftReview,
    mindmapDraftReview,
    moduleDecision,
  }
}

export default function ChatPage({ view, onChangeView }: PageProps) {
  const [sessions, setSessions] = useState<ChatSession[]>([])
  // sessions 的实时镜像，供 hydrate effect 读取当前会话的 mode（不进依赖数组）
  const sessionsRef = useRef(sessions)
  sessionsRef.current = sessions
  const [activeSessionId, setActiveSessionId] = useState<number | null>(null)
  const [taskMap, setTaskMap] = useState<Record<number, SessionState>>({})
  // taskMap 的实时镜像——供那些"每次 taskMap 变都不该重跑"的 effect 读取当前 slot，
  // 从而把 taskMap 移出依赖数组，避免 effect 自我中断（见 hydrate effect）。
  const taskMapRef = useRef(taskMap)
  taskMapRef.current = taskMap
  const [input, setInput] = useState('')
  const [larkDialogOpen, setLarkDialogOpen] = useState(false)
  const [pasteDialogOpen, setPasteDialogOpen] = useState(false)
  // 项目下所有模块——模块确认卡 / 知识草稿审核面板的下拉数据源。
  // 进入页面拉一次；新建模块后刷新。
  const [modules, setModules] = useState<ModuleSummary[]>([])
  const reloadModules = useCallback(() => {
    fetchModules().then(setModules).catch(err => console.error('Modules load:', err))
  }, [])
  // 当前要确认停止的会话 id；null 表示弹窗未开
  const [stopConfirmSid, setStopConfirmSid] = useState<number | null>(null)

  // 每个会话的"取消器"——把当前在跑任务的 abort 函数挂在 ref 上，
  // 用户点停止时一次性触发，不入 setState 避免重渲染。
  // 一个会话同时只可能有一个任务（busy 锁），所以不需要 list。
  const cancelMapRef = useRef<Map<number, () => void>>(new Map())
  const setCancel = useCallback((sid: number, fn: (() => void) | null) => {
    if (fn) cancelMapRef.current.set(sid, fn)
    else cancelMapRef.current.delete(sid)
  }, [])

  // 合并抽取的 latest-wins 守卫：补传第二份文档会再触发一次抽取，用 per-session 递增
  // token 保证只有最后一次的结果落面板，避免两次抽取竞态把旧结果覆盖新结果。
  const extractTokenRef = useRef<Map<number, number>>(new Map())

  const bottomRef = useRef<HTMLDivElement>(null)
  // 引导卡里的「上传文档」按钮通过这两个隐藏 input 触发，复用 handleFileSelect / handleMindmapSelect。
  const guideFileRef = useRef<HTMLInputElement>(null)
  const guideMindmapRef = useRef<HTMLInputElement>(null)
  // probe useEffect 里要在 generate 中断时静默重发，但 runGenerate 是后定义的 useCallback；
  // 用一个 ref 桥接，避免重新声明 effect 依赖时引发重新订阅。
  const runGenerateRef = useRef<((
    sid: number, documentId: number | null, mindmapDocumentId: number | null,
    rounds: ClarificationRoundHistory[],
    moduleName: string, casePrefix: string, knowledgeIds: number[] | null,
  ) => void) | null>(null)

  // 同理：脑图模式上传处理器在前定义，而 startMindmapClarification 在后；用 ref 桥接，
  // 让上传 onDone 能在脑图模式下自动触发首轮澄清。
  const startMindmapClarificationRef = useRef<((sid: number, documentId: number) => void) | null>(null)

  // Helper: update a single session's state by id. Idempotent — if the session
  // has been deleted from the map (shouldn't happen, but defensive), no-op.
  const patchSession = useCallback((sid: number, patch: Partial<SessionState> | ((s: SessionState) => Partial<SessionState>)) => {
    setTaskMap(prev => {
      const cur = prev[sid] ?? emptyState()
      const delta = typeof patch === 'function' ? patch(cur) : patch
      return { ...prev, [sid]: { ...cur, ...delta } }
    })
  }, [])

  // 上传完成后触发一次「PRD + 脑图」合并知识抽取（脑图优先）。后端把草稿落到主文档
  // 并清空副文档，返回 role 指明落到哪个审核 slot；另一个 slot 一并清空，保证只出一个面板。
  const triggerCombinedExtraction = useCallback(async (sid: number) => {
    const token = (extractTokenRef.current.get(sid) ?? 0) + 1
    extractTokenRef.current.set(sid, token)
    patchSession(sid, { extractingDrafts: true })
    try {
      const payload = await extractCombinedDrafts(sid)
      // 有更晚的抽取已经发起 → 丢弃本次结果（latest-wins）
      if (extractTokenRef.current.get(sid) !== token) return

      const drafts = payload.drafts || []
      const filename =
        payload.role === 'mindmap'
          ? (taskMapRef.current[sid]?.uploadMindmap?.filename ?? null)
          : (taskMapRef.current[sid]?.uploadResult?.filename ?? null)
      const slot =
        drafts.length > 0 && payload.document_id != null
          ? {
              documentId: payload.document_id,
              role: payload.role,
              filename,
              moduleName: payload.module_name,
              drafts,
              submitting: false,
            }
          : null
      patchSession(sid, {
        extractingDrafts: false,
        // 草稿只落一个 slot，另一个清空——合并后永远只出一个审核面板
        prdDraftReview: payload.role === 'prd' ? (slot as SessionState['prdDraftReview']) : null,
        mindmapDraftReview: payload.role === 'mindmap' ? (slot as SessionState['mindmapDraftReview']) : null,
      })
    } catch (err) {
      console.error('Combined extraction failed:', err)
      if (extractTokenRef.current.get(sid) === token) {
        patchSession(sid, { extractingDrafts: false })
      }
    }
  }, [patchSession])

  // 上传时 LLM 对模块的判定 → 一律弹「模块确认卡」（高置信自动命中也要用户确认）。
  // - applied=true：后端已把文档高置信落库到 suggested 模块；卡片默认选中它，用户可改选。
  // - proposed_module：LLM 建议新建模块；卡片默认进入"新建模块"分支并预填字段。
  // - 中置信命中既有模块：默认选中该模块。
  // 三种情况都需要用户拍板，确认后才把文档归入所选模块并解锁下方流程。
  const handleModuleAutoClassified = useCallback(
    (sid: number, payload: import('../api/client').ModuleAutoClassifiedPayload) => {
      const prop = payload.proposed_module
      const sug = payload.suggestion
      // 后端高置信 applied 时 payload.module_id 是落库模块；中置信时用 suggestion.module_id
      const hitModuleId = sug.applied ? payload.module_id : sug.module_id
      const hitModuleName = sug.applied ? payload.module_name : null
      patchSession(sid, {
        // 高置信已由后端落库 → 先把 confirmedModuleName/Id 记上，这样即便用户"忽略"确认卡，
        // 后续草稿审核面板也能默认选中真实归属模块（与后端一致）。
        ...(sug.applied && hitModuleName ? { confirmedModuleName: hitModuleName, confirmedModuleId: hitModuleId } : {}),
        moduleDecision: {
          documentId: payload.document_id ?? null,
          suggestedModuleId: hitModuleId,
          suggestedModuleName: hitModuleName,
          applied: sug.applied,
          confidence: sug.confidence,
          reasoning: sug.reasoning || '',
          // 命中既有模块 → 默认选它；只有提议新建 → 默认进新建分支
          selectedModuleId: hitModuleId ?? null,
          createNew: !hitModuleId && !!(prop && prop.name),
          createName: prop?.name || '',
          createCode: prop?.code || '',
          createDescription: prop?.description ?? null,
          creating: false,
        },
      })
    },
    [patchSession],
  )

  // 更新模块确认卡里的可编辑字段（下拉选中 / 新建模块的名字等）。
  const patchModuleDecision = useCallback((
    sid: number,
    patch: Partial<NonNullable<SessionState['moduleDecision']>>,
  ) => {
    patchSession(sid, prev => ({
      moduleDecision: prev.moduleDecision ? { ...prev.moduleDecision, ...patch } : null,
    }))
  }, [patchSession])

  // 「确认归类」：按用户在卡片里的选择把文档归入模块。
  //   - createNew：先建模块再把文档归入
  //   - selectedModuleId=某模块：把文档归入该模块（若与自动落库结果不同也纠正）
  //   - selectedModuleId=null 且非新建：不归入模块（项目级），若之前被自动落库则清空
  // 完成后清掉确认卡，记录 confirmedModuleName 供后续澄清/生成阶段展示。
  const confirmModuleDecision = useCallback(async (
    sid: number,
    decision: NonNullable<SessionState['moduleDecision']>,
  ) => {
    patchSession(sid, prev => ({
      moduleDecision: prev.moduleDecision ? { ...prev.moduleDecision, creating: true } : null,
    }))
    try {
      let finalModuleId: number | null
      let finalModuleName: string | null
      if (decision.createNew) {
        const created = await createModule({
          name: decision.createName.trim(),
          code: decision.createCode.trim() || null,
          description: decision.createDescription,
        })
        finalModuleId = created.id
        finalModuleName = created.name
      } else {
        finalModuleId = decision.selectedModuleId
        finalModuleName = finalModuleId != null
          ? (modules.find(m => m.id === finalModuleId)?.name ?? null)
          : null
      }
      // 把文档归入最终模块（null=不归入）。仅当与后端已落库结果不同才需要纠正，
      // 但无脑调一次也无害——后端幂等更新。documentId 为 null（脑图独立态）时跳过。
      if (decision.documentId != null) {
        await updateDocumentModule(decision.documentId, finalModuleId)
      }
      if (decision.createNew) reloadModules()
      patchSession(sid, {
        moduleDecision: null,
        confirmedModuleName: finalModuleName,
        confirmedModuleId: finalModuleId,
      })
    } catch (e) {
      console.error('Confirm module decision failed:', e)
      patchSession(sid, prev => ({
        moduleDecision: prev.moduleDecision ? { ...prev.moduleDecision, creating: false } : null,
      }))
    }
  }, [patchSession, modules, reloadModules])

  // 「忽略」：不改动文档归属（沿用后端当前状态），直接清掉确认卡继续流程。
  const dismissModuleDecision = useCallback((sid: number) => {
    patchSession(sid, { moduleDecision: null })
  }, [patchSession])

  // The currently-displayed session's state. Falls back to a fresh empty slot
  // so the UI never crashes when nothing is selected.
  const active: SessionState = useMemo(() => {
    if (activeSessionId == null) return emptyState()
    return taskMap[activeSessionId] ?? emptyState()
  }, [activeSessionId, taskMap])

  // 草稿审核面板"加入模块"的默认选中：优先用模块确认卡刚拍板的 confirmedModuleId
  // （新建模块时 modules 列表还没异步刷新到，用 id 才不丢）；没有 id 时（如刷新 hydrate
  // 只恢复了名字）退回按 confirmedModuleName 反查。都没有 → null（不归入模块）。
  const draftDefaultModuleId = useMemo(() => {
    if (active.confirmedModuleId != null) return active.confirmedModuleId
    if (active.confirmedModuleName != null) {
      return modules.find(m => m.name === active.confirmedModuleName)?.id ?? null
    }
    return null
  }, [active.confirmedModuleId, active.confirmedModuleName, modules])

  // Set of session ids with running work — surfaced in the sidebar.
  const busyIds = useMemo(() => {
    const s = new Set<number>()
    for (const [k, v] of Object.entries(taskMap)) {
      if (isBusy(v)) s.add(Number(k))
    }
    return s
  }, [taskMap])

  // Load sessions on mount
  useEffect(() => {
    fetchSessions().then(setSessions).catch(console.error)
  }, [])

  // Load module list on mount（模块确认卡 / 草稿审核面板下拉用）
  useEffect(() => {
    reloadModules()
  }, [reloadModules])

  // When a session becomes active, lazily load its messages + cases + clarification state.
  // We only fetch once per session (loaded flag), and we never clobber in-progress
  // task state — uploadResult/clarification/etc. stay put.
  useEffect(() => {
    if (activeSessionId == null) return
    // 用 ref 读 loaded，而不是把 taskMap 放进依赖数组：否则本 effect 里的 patchSession
    // 会改动 taskMap → 触发本 effect 重跑 → cleanup 把刚发出的知识预览请求 abort 掉，
    // catch 又因 cancelled 提前 return，导致 loading 永久卡住。
    const cur = taskMapRef.current[activeSessionId]
    if (cur?.loaded) return

    let cancelled = false
    // hydrate 阶段拉知识预览也要能被"停止任务"中断：把 controller 挂进 cancelMap，
    // 切会话/卸载时 cleanup 里 abort + 注销，避免请求泄漏。
    const previewController = new AbortController()
    Promise.all([
      fetchMessages(activeSessionId).catch(err => { console.error(err); return [] }),
      fetchSessionCases(activeSessionId).catch(err => { console.error(err); return { cases: [] as TestCase[] } }),
      fetchClarificationState(activeSessionId).catch(err => { console.error(err); return null }),
    ]).then(([msgs, casesRes, clarState]) => {
      if (cancelled) return
      const sessionMode = sessionsRef.current.find(s => s.id === activeSessionId)?.mode ?? 'cases'
      patchSession(activeSessionId, {
        mode: sessionMode,
        messages: msgs,
        testCases: casesRes.cases,
        loaded: true,
        ...(clarState ? stateFromDTO(clarState, casesRes.cases.length > 0) : {}),
      })
      // awaiting_clarification 路径下 stateFromDTO 给了一个 loading 占位面板，
      // 这里异步把 hits 拉回来填上，让用户能看到候选条目继续操作。
      if (clarState?.status === 'awaiting_clarification' && clarState.document_id != null) {
        setCancel(activeSessionId, () => previewController.abort())
        fetchKnowledgePreview(activeSessionId, undefined, previewController.signal).then(preview => {
          if (cancelled) return
          patchSession(activeSessionId, prev => (
            prev.knowledgePreview && prev.knowledgePreview.phase === 'clarify'
              ? { knowledgePreview: { ...prev.knowledgePreview, loading: false, hits: preview.hits } }
              : {}
          ))
        }).catch(err => {
          const aborted =
            previewController.signal.aborted
            || (err as { name?: string; code?: string })?.name === 'CanceledError'
            || (err as { code?: string })?.code === 'ERR_CANCELED'
          if (!aborted) console.error('Knowledge preview hydrate failed:', err)
          if (cancelled) return
          patchSession(activeSessionId, prev => (
            prev.knowledgePreview && prev.knowledgePreview.phase === 'clarify'
              ? { knowledgePreview: { ...prev.knowledgePreview, loading: false, hits: [] } }
              : {}
          ))
        }).finally(() => setCancel(activeSessionId, null))
      }
    })

    return () => {
      cancelled = true
      previewController.abort()
    }
  }, [activeSessionId, patchSession, setCancel])

  // Auto-scroll on message or stream change for the active session.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [active.messages, active.streamBuffer])

  // 后端"待生成 / 生成中"的恢复探测：
  //   1. fetch 在锁屏期间被中断 → catch 已经把 pendingGenerate 落了；
  //   2. 用户切走再切回来 → visibilitychange/focus 触发回查；
  //   3. 后端 generate 还在跑（fetch 已断但 server 进程未终止）→ 每 5s 轮询 cases；
  //   4. 探测到后端 cases 已落库 → 自动 hydrate；
  //   5. 探测到后端 state="generating" 但 cases 长期为空、且本地没有正在跑的 generate
  //      → 静默自动重发一次 generate 请求（用户全程只看到 loader）。
  useEffect(() => {
    if (activeSessionId == null) return
    const sid = activeSessionId
    const cur = taskMap[sid]
    if (!cur) return

    // 触发回查的条件：本会话还没看到用例，但后端可能在/已经在生成
    const shouldProbe = cur.loaded && cur.testCases.length === 0 && (cur.generating || cur.pendingGenerate != null)
    if (!shouldProbe) return

    let stopped = false
    // 同一会话最多自动重发一次，避免 generate 路由本身有 bug 时陷入死循环
    let autoResumed = false

    const probe = async () => {
      if (stopped) return
      if (document.visibilityState !== 'visible') return
      try {
        const [casesRes, clarState, msgs] = await Promise.all([
          fetchSessionCases(sid).catch(() => ({ cases: [] as TestCase[] })),
          fetchClarificationState(sid).catch(() => null),
          fetchMessages(sid).catch(() => null),
        ])
        if (stopped) return
        if (casesRes.cases.length > 0) {
          // 后端跑完了——hydrate 用例 + 重拉 messages 把 generate_done 气泡补上
          patchSession(sid, prev => ({
            testCases: casesRes.cases,
            messages: msgs ?? prev.messages,
            uploadResult: null,
            confirmedModuleName: null,
            confirmedCasePrefix: null,
            clarificationRounds: [],
            currentQuestions: null,
            currentSummary: '',
            currentRound: 1,
            followupActive: false,
            followupBuffer: '',
            generating: false,
            pendingGenerate: null,
          }))
          return
        }
        if (
          clarState
          && clarState.status === 'generating'
          && clarState.confirmed_module_name
          && clarState.confirmed_case_prefix
          && (clarState.document_id != null || clarState.mindmap_document_id != null)
        ) {
          const pending = {
            documentId: clarState.document_id,
            mindmapDocumentId: clarState.mindmap_document_id,
            moduleName: clarState.confirmed_module_name,
            casePrefix: clarState.confirmed_case_prefix,
            rounds: clarState.rounds,
          }
          // 确保 pendingGenerate 一直填着，让 UI 显示常驻 loader
          patchSession(sid, prev => ({
            messages: msgs ?? prev.messages,
            pendingGenerate: prev.pendingGenerate ?? pending,
          }))
          // 关键：本地没有正在跑的 generate（fetch 已断/重进会话）→ 静默重发
          // 后端的 generate 路由在用例落库前没有"占位"机制，重发是幂等的（同一会话会再生成一批，
          // 但实测下游 routes_generate.py 只在 commit 后追加，新一轮调用会替换前端展示的最终结果）
          if (!autoResumed && !cur.generating) {
            autoResumed = true
            // 自动恢复：传 null 让后端走老 top-K 路径（用户没机会勾选，保守用全量）
            runGenerateRef.current?.(
              sid, pending.documentId, pending.mindmapDocumentId,
              pending.rounds, pending.moduleName, pending.casePrefix, null,
            )
          }
        }
      } catch (e) {
        console.error('Generate probe failed:', e)
      }
    }

    // 立即探一次（处理切回来 / 刷新后的首次回填）
    probe()
    // 之后每 5s 探一次直到拿到用例
    const interval = window.setInterval(probe, 5000)
    const onVisible = () => { if (document.visibilityState === 'visible') probe() }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onVisible)
    return () => {
      stopped = true
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', onVisible)
    }
  }, [activeSessionId, taskMap, patchSession])

  // ── Session actions ──────────────────────────────────────────────────────

  // 用户在侧边栏选定模式后创建会话并进入。mode 固定该会话用途。
  const handleCreateSession = useCallback(async (mode: 'cases' | 'mindmap') => {
    const s = await createSession('新会话', undefined, mode)
    setSessions(prev => [s, ...prev])
    setTaskMap(prev => ({ ...prev, [s.id]: { ...emptyState(mode), loaded: true } }))
    setActiveSessionId(s.id)
  }, [])

  const handleSelectSession = useCallback((id: number) => {
    // 切换不取消任何在跑任务，也不清空 state — 只是换视角。
    setActiveSessionId(id)
  }, [])

  const handleDeleteSession = useCallback(async (id: number) => {
    // 先中断该会话可能在跑的任务（导入/生成），再删。
    const cancel = cancelMapRef.current.get(id)
    if (cancel) {
      cancelMapRef.current.delete(id)
      try { cancel() } catch { /* ignore */ }
    }
    try {
      await deleteSession(id)
    } catch (err) {
      console.error('Delete session failed:', err)
      window.alert('删除会话失败，请重试。')
      return
    }
    setSessions(prev => prev.filter(s => s.id !== id))
    setTaskMap(prev => {
      const next = { ...prev }
      delete next[id]
      return next
    })
    // 删的是当前会话 → 切到剩余第一个（或 null）
    setActiveSessionId(prev => {
      if (prev !== id) return prev
      const rest = sessions.filter(s => s.id !== id)
      return rest.length > 0 ? rest[0].id : null
    })
  }, [sessions])

  // ── Chat streaming ───────────────────────────────────────────────────────

  const handleSend = useCallback(() => {
    const text = input.trim()
    if (!text || active.streaming || activeSessionId == null && false) return
    setInput('')

    const startSid = activeSessionId  // 可能为 null —— 让后端创建新会话

    // 乐观插入 user 消息（在已有 sid 的会话里）
    const tempUserMsg: IChatMessage = {
      id: Date.now(),
      role: 'user',
      content: text,
      created_at: new Date().toISOString(),
    }
    if (startSid != null) {
      patchSession(startSid, prev => ({
        messages: [...prev.messages, tempUserMsg],
        streaming: true,
        streamBuffer: '',
      }))
    }

    const abort = streamChat(
      text,
      startSid,
      (chunk) => {
        // sid 在 onDone 之前可能还未知 —— 写到 startSid（已知）或先缓存到一个占位 slot。
        if (startSid != null) {
          patchSession(startSid, prev => ({ streamBuffer: prev.streamBuffer + chunk }))
        }
        // 若 startSid 为 null（无会话发首条消息），UI 不显示中间 buffer，
        // 等 onDone 拿到 newSessionId 再统一刷消息列表 —— 这种 case 罕见且短暂。
      },
      (newSessionId) => {
        // 若是新建会话，激活它并刷会话列表
        if (startSid == null) {
          setActiveSessionId(newSessionId)
          fetchSessions().then(setSessions).catch(console.error)
        }
        patchSession(newSessionId, { streaming: false, streamBuffer: '' })
        setCancel(newSessionId, null)
        fetchMessages(newSessionId).then(msgs => {
          patchSession(newSessionId, { messages: msgs, loaded: true })
        }).catch(console.error)
        fetchSessions().then(setSessions).catch(console.error)
      },
      (err) => {
        console.error('Stream error:', err)
        if (startSid != null) {
          patchSession(startSid, { streaming: false, streamBuffer: '' })
          setCancel(startSid, null)
        }
      },
    )
    if (startSid != null) setCancel(startSid, abort)
  }, [input, active.streaming, activeSessionId, patchSession, setCancel])

  // ── Upload + clarification ───────────────────────────────────────────────

  const handleFileSelect = useCallback(async (file: File) => {
    // 自动用文档名命名会话（无会话时创建一个）。
    const baseName = file.name.replace(/\.(docx|pdf)$/i, '').slice(0, 60) || file.name
    let sid = activeSessionId
    try {
      if (sid == null) {
        const s = await createSession(baseName)
        setSessions(prev => [s, ...prev])
        setTaskMap(prev => ({ ...prev, [s.id]: { ...emptyState(), loaded: true } }))
        setActiveSessionId(s.id)
        sid = s.id
      } else {
        const cur = sessions.find(x => x.id === sid)
        if (cur && (cur.title === '新会话' || cur.title === 'New Session')) {
          const updated = await renameSession(sid, baseName)
          setSessions(prev => prev.map(s => (s.id === sid ? { ...s, title: updated.title } : s)))
        }
      }
    } catch (err) {
      console.error('Session naming failed:', err)
    }

    if (sid == null) return  // createSession 失败的兜底

    const startSid = sid

    // 锁定一份这次上传的 stage / token 累积器（不依赖最新 state，避免回调串台）
    const stages: string[] = []
    let llmBuffer = ''
    // 记住暂存/解析出的 document_id 供 onDone 用（不读异步的 taskMapRef，见 runPrd 同款注释）
    let stagedDocId: number | null = null
    const renderProgress = () =>
      stages.join('\n') + (llmBuffer ? `\n\n\`\`\`\n${llmBuffer}\n\`\`\`` : '')

    // 清掉这个会话之前可能残留的 PRD 上传/澄清态，开新一轮（保留 uploadMindmap）
    patchSession(startSid, {
      uploading: true,
      uploadStage: 'starting',
      uploadProgress: '',
      uploadResult: null,
      confirmedModuleName: null,
      confirmedCasePrefix: null,
      clarificationRounds: [],
      currentQuestions: null,
      currentSummary: '',
      currentRound: 1,
      followupActive: false,
      followupBuffer: '',
      knowledgePreview: null,
      pendingGenerate: null,
      prdDraftReview: null,
    })

    const abort = streamUpload(file, startSid, {
      onStage: (stage, message) => {
        stages.push(`▸ ${message}`)
        patchSession(startSid, { uploadStage: stage, uploadProgress: renderProgress() })
      },
      onToken: (text) => {
        llmBuffer += text
        patchSession(startSid, { uploadProgress: renderProgress() })
      },
      onResult: (result) => {
        // 兼容保留：旧「上传即澄清」路径才发 result。新流程上传只暂存、走 onStaged。
        stagedDocId = result.document_id ?? null
        patchSession(startSid, {
          uploadResult: result,
          confirmedModuleName: null,
          confirmedCasePrefix: null,
          clarificationRounds: [],
          currentQuestions: result.clarification?.questions ?? null,
          currentSummary: result.clarification?.summary ?? '',
          currentRound: 1,
          followupActive: false,
          followupBuffer: '',
          knowledgePreview: null,
        })
      },
      onStaged: (staged) => {
        // 新流程：文档已暂存（解析入库、未跑任何大模型）。落一个 uploadResult 空壳做 chip
        // 哨兵；不设 knowledgePreview / 不弹任何面板。用户点「开始生成」才进入下游流程。
        stagedDocId = staged.document_id
        const fakeUpload: UploadResult = {
          document_id: staged.document_id,
          filename: staged.filename,
          stats: staged.stats,
          clarification: {
            summary: '',
            module_detected: '',
            questions: [],
            ready_to_generate: false,
          },
        }
        patchSession(startSid, {
          uploadResult: fakeUpload,
          confirmedModuleName: null,
          confirmedCasePrefix: null,
          clarificationRounds: [],
          currentQuestions: null,
          currentSummary: '',
          currentRound: 1,
          followupActive: false,
          followupBuffer: '',
          knowledgePreview: null,
        })
      },
      onAssistantMessage: (msg) => {
        patchSession(startSid, prev => ({ messages: [...prev.messages, msg] }))
      },
      onError: (msg) => {
        console.error('Upload error:', msg)
        patchSession(startSid, {
          uploading: false,
          uploadStage: null,
          uploadProgress: '',
        })
        setCancel(startSid, null)
      },
      onDone: () => {
        // 上传只暂存：不再自动触发知识抽取 / 模块分类 / 澄清。等用户点「开始生成」。
        patchSession(startSid, { uploading: false, uploadStage: null, uploadProgress: '' })
        setCancel(startSid, null)
        // 脑图模式例外：上传的 PRD 暂存完成后，直接进入澄清（知识库命中 + 多轮），
        // 澄清就绪后再露出「生成测试脑图」卡。用例模式仍走「开始生成」闸门，不受影响。
        // mode 用同步 sessionsRef 读；docId 用闭包变量（不读异步的 taskMapRef）。
        const mode = sessionsRef.current.find(s => s.id === startSid)?.mode ?? 'cases'
        if (mode === 'mindmap' && stagedDocId != null) {
          startMindmapClarificationRef.current?.(startSid, stagedDocId)
        }
      },
    })
    setCancel(startSid, abort)
  }, [activeSessionId, sessions, patchSession, setCancel])

  // ── Mindmap upload ───────────────────────────────────────────────────────
  // 与 handleFileSelect 类似，但落到 uploadMindmap 这个并行 slot —— PRD upload 状态不被清掉。
  const handleMindmapSelect = useCallback(async (file: File) => {
    const baseName = file.name.replace(/\.(md|markdown)$/i, '').slice(0, 60) || file.name
    let sid = activeSessionId
    try {
      if (sid == null) {
        const s = await createSession(baseName)
        setSessions(prev => [s, ...prev])
        setTaskMap(prev => ({ ...prev, [s.id]: { ...emptyState(), loaded: true } }))
        setActiveSessionId(s.id)
        sid = s.id
      } else {
        const cur = sessions.find(x => x.id === sid)
        if (cur && (cur.title === '新会话' || cur.title === 'New Session')) {
          const updated = await renameSession(sid, baseName)
          setSessions(prev => prev.map(s => (s.id === sid ? { ...s, title: updated.title } : s)))
        }
      }
    } catch (err) {
      console.error('Session naming failed:', err)
    }

    if (sid == null) return

    const startSid = sid
    const stages: string[] = []
    let llmBuffer = ''
    const renderProgress = () =>
      stages.join('\n') + (llmBuffer ? `\n\n\`\`\`\n${llmBuffer}\n\`\`\`` : '')

    // 脑图重传时，把当前的 followup/澄清态清零（脑图变更后澄清/生成结果都需要重算），
    // 但保留 uploadResult（PRD 仍然有效）
    patchSession(startSid, {
      uploading: true,
      uploadStage: 'starting',
      uploadProgress: '',
      uploadMindmap: null,
      confirmedModuleName: null,
      confirmedCasePrefix: null,
      clarificationRounds: [],
      currentQuestions: null,
      currentSummary: '',
      currentRound: 1,
      followupActive: false,
      followupBuffer: '',
      knowledgePreview: null,
      pendingGenerate: null,
      mindmapDraftReview: null,
    })

    const abort = streamMindmapUpload(file, startSid, {
      onStage: (stage, message) => {
        stages.push(`▸ ${message}`)
        patchSession(startSid, { uploadStage: stage, uploadProgress: renderProgress() })
      },
      onToken: (text) => {
        llmBuffer += text
        patchSession(startSid, { uploadProgress: renderProgress() })
      },
      onResult: (result) => {
        // 兼容保留：旧路径 result；新流程脑图上传走 onStaged。
        const mindmapId = result.mindmap_document_id ?? result.document_id
        if (mindmapId == null) return
        patchSession(startSid, prev => ({
          uploadMindmap: {
            documentId: mindmapId,
            filename: result.filename,
            stats: result.stats,
          },
          knowledgePreview: null,
          ...(result.clarification && result.clarification.questions && result.clarification.questions.length > 0
            ? {
                currentQuestions: result.clarification.questions,
                currentSummary: result.clarification.summary,
                currentRound: 1,
              }
            : {}),
          ...(prev.uploadResult ? {} : {}),
        }))
      },
      onStaged: (staged) => {
        // 新流程：脑图已暂存。落 uploadMindmap 槽位，不弹面板；用户点「开始生成」再进入下游。
        patchSession(startSid, {
          uploadMindmap: {
            documentId: staged.document_id,
            filename: staged.filename,
            stats: staged.stats,
          },
          confirmedModuleName: null,
          confirmedCasePrefix: null,
          clarificationRounds: [],
          currentQuestions: null,
          currentSummary: '',
          currentRound: 1,
          followupActive: false,
          followupBuffer: '',
          knowledgePreview: null,
        })
      },
      onAssistantMessage: (msg) => {
        patchSession(startSid, prev => ({ messages: [...prev.messages, msg] }))
      },
      onError: (msg) => {
        console.error('Mindmap upload error:', msg)
        patchSession(startSid, {
          uploading: false,
          uploadStage: null,
          uploadProgress: '',
        })
        setCancel(startSid, null)
      },
      onDone: () => {
        patchSession(startSid, { uploading: false, uploadStage: null, uploadProgress: '' })
        setCancel(startSid, null)
      },
    })
    setCancel(startSid, abort)
  }, [activeSessionId, sessions, patchSession, setCancel])

  // ── Mindmap paste ────────────────────────────────────────────────────────
  // 粘贴的 Markdown 大纲封装成虚拟 File，复用 handleMindmapSelect 走同一条上传链路（后端零改动）。
  const handleMindmapPaste = useCallback((text: string, filename: string) => {
    setPasteDialogOpen(false)
    const safeBase = filename.replace(/\.(md|markdown)$/i, '').slice(0, 60) || '粘贴的脑图'
    const file = new File([text], `${safeBase}.md`, { type: 'text/markdown' })
    handleMindmapSelect(file)
  }, [handleMindmapSelect])


  // 单对话框双输入：PRD URL 和脑图 URL 都可填，至少填一个。
  // 实现策略：把 PRD / 脑图各自的 SSE 流封装成 Promise，串行执行（先 PRD 后脑图），
  // 两份独立写入 uploadResult / uploadMindmap，最终的 knowledgePreview 合并两个 doc id。
  const handleLarkImport = useCallback(async (urls: LarkUrlSubmit) => {
    setLarkDialogOpen(false)
    const { prdUrl, mindmapUrl } = urls
    if (!prdUrl && !mindmapUrl) return

    // 用 PRD URL（优先）或脑图 URL 末段做临时标题占位
    const seedUrl = prdUrl || mindmapUrl || ''
    const tail = seedUrl.split('/').filter(Boolean).pop() || '飞书文档'
    const fallbackTitle = `飞书：${tail}`.slice(0, 60)

    let sid = activeSessionId
    // 记录本次导入是否为此新建了会话——若导入从未产出任何内容就失败，
    // 把这个空会话删掉，避免反复重试在左侧堆积同名孤儿会话。
    let createdNewSid = false
    try {
      if (sid == null) {
        const s = await createSession(fallbackTitle)
        setSessions(prev => [s, ...prev])
        setTaskMap(prev => ({ ...prev, [s.id]: { ...emptyState(), loaded: true } }))
        setActiveSessionId(s.id)
        sid = s.id
        createdNewSid = true
      } else {
        const cur = sessions.find(x => x.id === sid)
        if (cur && (cur.title === '新会话' || cur.title === 'New Session')) {
          const updated = await renameSession(sid, fallbackTitle)
          setSessions(prev => prev.map(s => (s.id === sid ? { ...s, title: updated.title } : s)))
        }
      }
    } catch (err) {
      console.error('Session naming failed:', err)
    }

    if (sid == null) return
    const startSid = sid

    // PRD 阶段：清掉已有的 PRD/澄清态，启动 streamLarkImport，onDone 时 resolve。
    const runPrd = (url: string) => new Promise<void>(resolve => {
      const stages: string[] = []
      let llmBuffer = ''
      // 是否已从后端收到过实质内容（result / 知识预览 / 草稿）。用于判断导入失败时
      // 该会话是不是"从没成过的空壳"——是则删掉，避免堆积孤儿会话。
      let gotContent = false
      // 记住暂存的 PRD document_id。onDone 里不能读 taskMapRef（setTaskMap 异步批处理，
      // onStaged 刚 patch 的值此刻可能还没进 ref），用闭包变量才可靠。
      let stagedDocId: number | null = null
      const renderProgress = () =>
        stages.join('\n') + (llmBuffer ? `\n\n\`\`\`\n${llmBuffer}\n\`\`\`` : '')

      patchSession(startSid, {
        uploading: true,
        uploadStage: 'starting',
        uploadProgress: '',
        uploadResult: null,
        confirmedModuleName: null,
        confirmedCasePrefix: null,
        clarificationRounds: [],
        currentQuestions: null,
        currentSummary: '',
        currentRound: 1,
        followupActive: false,
        followupBuffer: '',
        knowledgePreview: null,
        pendingGenerate: null,
        prdDraftReview: null,
      })

      const abort = streamLarkImport(url, startSid, {
        onStage: (stage, message) => {
          stages.push(`▸ ${message}`)
          patchSession(startSid, { uploadStage: stage, uploadProgress: renderProgress() })
        },
        onToken: (text) => {
          llmBuffer += text
          patchSession(startSid, { uploadProgress: renderProgress() })
        },
        onResult: (result) => {
          gotContent = true
          // 兼容保留：旧路径 result；新流程飞书 PRD 走 onStaged。
          patchSession(startSid, prev => ({
            uploadResult: result,
            confirmedModuleName: null,
            confirmedCasePrefix: null,
            clarificationRounds: [],
            currentQuestions: mindmapUrl ? null : (result.clarification?.questions ?? null),
            currentSummary: mindmapUrl ? '' : (result.clarification?.summary ?? ''),
            currentRound: 1,
            followupActive: false,
            followupBuffer: '',
            knowledgePreview: prev.knowledgePreview,
          }))
          const cur = sessions.find(x => x.id === startSid)
          if (cur && (cur.title === fallbackTitle || cur.title === '新会话') && result.filename) {
            renameSession(startSid, result.filename.slice(0, 60))
              .then(updated => {
                setSessions(prev => prev.map(s => (s.id === startSid ? { ...s, title: updated.title } : s)))
              })
              .catch(err => console.error('Rename after lark import failed:', err))
          }
        },
        onStaged: (staged) => {
          gotContent = true
          stagedDocId = staged.document_id
          // 新流程：飞书 PRD 已暂存。落 uploadResult 空壳做 chip 哨兵，不弹面板。
          const fakeUpload: UploadResult = {
            document_id: staged.document_id,
            filename: staged.filename,
            stats: staged.stats,
            clarification: {
              summary: '',
              module_detected: '',
              questions: [],
              ready_to_generate: false,
            },
          }
          patchSession(startSid, {
            uploadResult: fakeUpload,
            confirmedModuleName: null,
            confirmedCasePrefix: null,
            clarificationRounds: [],
            currentQuestions: null,
            currentSummary: '',
            currentRound: 1,
            followupActive: false,
            followupBuffer: '',
            knowledgePreview: null,
          })
          const cur = sessions.find(x => x.id === startSid)
          if (cur && (cur.title === fallbackTitle || cur.title === '新会话') && staged.filename) {
            renameSession(startSid, staged.filename.slice(0, 60))
              .then(updated => {
                setSessions(prev => prev.map(s => (s.id === startSid ? { ...s, title: updated.title } : s)))
              })
              .catch(err => console.error('Rename after lark import failed:', err))
          }
        },
        onModuleAutoClassified: (payload) => handleModuleAutoClassified(startSid, payload),
        onAssistantMessage: (msg) => {
          patchSession(startSid, prev => ({ messages: [...prev.messages, msg] }))
        },
        onError: (msg) => {
          console.error('Lark import error:', msg)
          setCancel(startSid, null)
          // 这次是为导入新建的空会话、全程没收到任何内容、且后面没有脑图阶段要跑
          // → 直接删掉这个孤儿会话，避免反复重试在左侧堆积同名空会话。
          if (createdNewSid && !gotContent && !mindmapUrl) {
            deleteSession(startSid).catch(err => console.error('Cleanup orphan session failed:', err))
            setSessions(prev => prev.filter(s => s.id !== startSid))
            setTaskMap(prev => {
              const next = { ...prev }
              delete next[startSid]
              return next
            })
            setActiveSessionId(prev => (prev === startSid ? null : prev))
            resolve()
            return
          }
          // 流被中断（后端重启 / 网络断）时给用户一条可见提示，避免聊天区静默变空白。
          patchSession(startSid, prev => ({
            uploading: false, uploadStage: null, uploadProgress: '',
            messages: [...prev.messages, {
              id: Date.now(),
              role: 'assistant',
              content: `❌ 飞书导入中断：${msg}\n\n可能是后端重启或网络中断，请重新点击导入重试。`,
              created_at: new Date().toISOString(),
            }],
          }))
          resolve()
        },
        onDone: () => {
          // 新流程：飞书导入只暂存，不自动抽取；用户点「开始生成」再进入下游。
          patchSession(startSid, { uploading: false, uploadStage: null, uploadProgress: '' })
          setCancel(startSid, null)
          // 脑图模式（mindmapOnly 对话框只收 PRD 链接）：暂存完成即进入澄清。
          // mode 用同步的 sessionsRef 读；docId 用闭包变量（不读异步的 taskMapRef）。
          const mode = sessionsRef.current.find(s => s.id === startSid)?.mode ?? 'cases'
          if (mode === 'mindmap' && stagedDocId != null) {
            startMindmapClarificationRef.current?.(startSid, stagedDocId)
          }
          resolve()
        },
      })
      setCancel(startSid, abort)
    })

    // 脑图阶段：保留 uploadResult，重置 uploadMindmap 与澄清态。
    const runMindmap = (url: string) => new Promise<void>(resolve => {
      const stages: string[] = []
      let llmBuffer = ''
      const renderProgress = () =>
        stages.join('\n') + (llmBuffer ? `\n\n\`\`\`\n${llmBuffer}\n\`\`\`` : '')

      patchSession(startSid, {
        uploading: true,
        uploadStage: 'starting',
        uploadProgress: '',
        uploadMindmap: null,
        confirmedModuleName: null,
        confirmedCasePrefix: null,
        clarificationRounds: [],
        currentQuestions: null,
        currentSummary: '',
        currentRound: 1,
        followupActive: false,
        followupBuffer: '',
        knowledgePreview: null,
        pendingGenerate: null,
        mindmapDraftReview: null,
      })

      const abort = streamLarkMindmapImport(url, startSid, {
        onStage: (stage, message) => {
          stages.push(`▸ ${message}`)
          patchSession(startSid, { uploadStage: stage, uploadProgress: renderProgress() })
        },
        onToken: (text) => {
          llmBuffer += text
          patchSession(startSid, { uploadProgress: renderProgress() })
        },
        onResult: (result) => {
          const mindmapId = result.mindmap_document_id ?? result.document_id
          if (mindmapId == null) return
          patchSession(startSid, {
            uploadMindmap: {
              documentId: mindmapId,
              filename: result.filename,
              stats: result.stats,
            },
          })
        },
        onStaged: (staged) => {
          // 新流程：飞书脑图已暂存。落 uploadMindmap 槽位，不弹面板。
          patchSession(startSid, {
            uploadMindmap: {
              documentId: staged.document_id,
              filename: staged.filename,
              stats: staged.stats,
            },
            confirmedModuleName: null,
            confirmedCasePrefix: null,
            clarificationRounds: [],
            currentQuestions: null,
            currentSummary: '',
            currentRound: 1,
            followupActive: false,
            followupBuffer: '',
            knowledgePreview: null,
          })
          // 仅脑图模式（无 PRD）时用脑图 title 命名会话
          if (!prdUrl) {
            const cur = sessions.find(x => x.id === startSid)
            if (cur && (cur.title === fallbackTitle || cur.title === '新会话') && staged.filename) {
              renameSession(startSid, staged.filename.slice(0, 60))
                .then(updated => {
                  setSessions(prev => prev.map(s => (s.id === startSid ? { ...s, title: updated.title } : s)))
                })
                .catch(err => console.error('Rename after lark mindmap import failed:', err))
            }
          }
        },
        onModuleAutoClassified: (payload) => handleModuleAutoClassified(startSid, payload),
        onAssistantMessage: (msg) => {
          patchSession(startSid, prev => ({ messages: [...prev.messages, msg] }))
        },
        onError: (msg) => {
          console.error('Lark mindmap import error:', msg)
          patchSession(startSid, prev => ({
            uploading: false, uploadStage: null, uploadProgress: '',
            messages: [...prev.messages, {
              id: Date.now(),
              role: 'assistant',
              content: `❌ 飞书脑图导入中断：${msg}\n\n可能是后端重启或网络中断，请重新点击导入重试。`,
              created_at: new Date().toISOString(),
            }],
          }))
          setCancel(startSid, null)
          resolve()
        },
        onDone: () => {
          // 新流程：飞书脑图导入只暂存，不自动抽取；用户点「开始生成」再进入下游。
          patchSession(startSid, { uploading: false, uploadStage: null, uploadProgress: '' })
          setCancel(startSid, null)
          resolve()
        },
      })
      setCancel(startSid, abort)
    })

    if (prdUrl) await runPrd(prdUrl)
    if (mindmapUrl) await runMindmap(mindmapUrl)
  }, [activeSessionId, sessions, patchSession, setCancel, handleModuleAutoClassified])

  const runGenerate = useCallback(async (
    sid: number,
    documentId: number | null,
    mindmapDocumentId: number | null,
    rounds: ClarificationRoundHistory[],
    moduleName: string,
    casePrefix: string,
    knowledgeIds: number[] | null,
  ) => {
    patchSession(sid, { generating: true, knowledgePreview: null })
    // 把 generate 的 AbortController 也注册进 cancelMap，让"停止"按钮可中止 axios 请求
    const controller = new AbortController()
    setCancel(sid, () => controller.abort())
    const flatAnswers: Record<string, string> = {}
    rounds.forEach(rnd => {
      rnd.questions.forEach(q => {
        const a = rnd.answers[String(q.id)]
        if (a) flatAnswers[q.question] = a
      })
    })
    try {
      const result = await generateCases(
        sid, documentId, flatAnswers, moduleName, casePrefix, knowledgeIds, mindmapDocumentId,
        controller.signal,
      )
      patchSession(sid, prev => {
        // 把"已确认 N 条 / 未注入"的系统气泡 + "已生成 N 条用例"的系统气泡顺序 append；
        // 两者后端都已落库，刷新后从 fetchMessages 能读出来。
        const extra: IChatMessage[] = []
        if (result.knowledge_selection_message) extra.push(result.knowledge_selection_message)
        if (result.assistant_message) extra.push(result.assistant_message)
        return {
          testCases: result.cases,
          messages: extra.length > 0 ? [...prev.messages, ...extra] : prev.messages,
          uploadResult: null,
          uploadMindmap: null,
          confirmedModuleName: null,
          confirmedCasePrefix: null,
          clarificationRounds: [],
          currentQuestions: null,
          currentSummary: '',
          currentRound: 1,
          followupActive: false,
          followupBuffer: '',
          knowledgePreview: null,
          pendingGenerate: null,
        }
      })
    } catch (err) {
      // 用户主动点了"停止任务" → axios 抛 CanceledError；这种情况下不需要 probe，
      // 也不该挂 pendingGenerate（用户的本意是中止，"继续生成"按钮反而打扰人）。
      // 让用户回到澄清完成的中间态——他可以重新点"开始生成"按钮再来一次。
      const aborted =
        controller.signal.aborted
        || (err as { name?: string; code?: string })?.name === 'CanceledError'
        || (err as { code?: string })?.code === 'ERR_CANCELED'
      if (aborted) {
        patchSession(sid, {
          pendingGenerate: { documentId, mindmapDocumentId, moduleName, casePrefix, rounds },
        })
        return
      }

      // 锁屏 / 网络抖动 / 浏览器 tab 节流都可能让 fetch 抛错。
      // 但后端那次 generate 实际可能已经跑完了（用例已落库 + state 切到 done）。
      // 这里主动回查一次：跑完了就直接 hydrate；没跑完就放出「继续生成」按钮，避免 UI 静默卡死。
      console.error('Generate error:', err)
      try {
        const [casesRes, clarState] = await Promise.all([
          fetchSessionCases(sid),
          fetchClarificationState(sid),
        ])
        if (casesRes.cases.length > 0 && clarState?.status === 'done') {
          // 后端其实成功了，前端只是没拿到 response —— 直接接管已落库的结果
          // 注意：generate 路由写的 assistant_message 这里读不到（在 fetchMessages 里），
          // 用户切走再切回来或刷新就能看到。这里至少把表格补上不让人困惑。
          patchSession(sid, prev => ({
            testCases: casesRes.cases,
            uploadResult: null,
            uploadMindmap: null,
            confirmedModuleName: null,
            confirmedCasePrefix: null,
            clarificationRounds: [],
            currentQuestions: null,
            currentSummary: '',
            currentRound: 1,
            followupActive: false,
            followupBuffer: '',
            knowledgePreview: null,
            pendingGenerate: null,
            messages: prev.messages,
          }))
          // 顺便把 message 列表也重拉一次，把后端写的 generate_done 气泡补上
          fetchMessages(sid).then(msgs => patchSession(sid, { messages: msgs })).catch(console.error)
        } else {
          // 真没跑完：保留澄清入参，让用户点「继续生成」再跑一次
          patchSession(sid, {
            pendingGenerate: { documentId, mindmapDocumentId, moduleName, casePrefix, rounds },
          })
        }
      } catch (e2) {
        console.error('Generate recovery probe failed:', e2)
        // 回查也挂了——最低限度给个按钮兜底
        patchSession(sid, {
          pendingGenerate: { documentId, mindmapDocumentId, moduleName, casePrefix, rounds },
        })
      }
    } finally {
      patchSession(sid, { generating: false })
      setCancel(sid, null)
    }
  }, [patchSession, setCancel])

  // 把最新版 runGenerate 挂到 ref，让 probe useEffect 能调用它而不用进依赖项
  runGenerateRef.current = runGenerate

  // 从当前会话的 PRD 文档生成测试脑图并写入飞书。独立于用例生成，随时可点。
  const runGenerateMindmap = useCallback(async (sid: number, documentId: number) => {
    patchSession(sid, { mindmapGenerating: true, mindmapResult: null })
    // 把多轮澄清答案拍平成 {问题文本: 答案}，随生成请求注入（与 runGenerate 同口径，见上方）。
    const cur = taskMapRef.current[sid]
    const flatAnswers: Record<string, string> = {}
    ;(cur?.clarificationRounds ?? []).forEach(rnd => {
      rnd.questions.forEach(q => {
        const a = rnd.answers[String(q.id)]
        if (a) flatAnswers[q.question] = a
      })
    })
    try {
      const res = await generateMindmap(documentId, {
        sessionId: sid,
        moduleName: (cur?.confirmedModuleName ?? active.confirmedModuleName) ?? undefined,
        moduleId: (cur?.confirmedModuleId ?? active.confirmedModuleId) ?? undefined,
        clarifications: Object.keys(flatAnswers).length > 0 ? flatAnswers : undefined,
      })
      patchSession(sid, prev => ({
        mindmapResult: { url: res.url, title: res.title },
        // 后端带 session_id 时会落一条 assistant 气泡（含飞书链接），append 到聊天流
        messages: res.assistant_message ? [...prev.messages, res.assistant_message] : prev.messages,
      }))
    } catch (err) {
      console.error('Generate mindmap error:', err)
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      const content = detail ? `生成测试脑图失败：${detail}` : '生成测试脑图失败，请稍后重试。'
      // 失败也给用户一条可见反馈（本地气泡，不落库）
      patchSession(sid, prev => ({
        messages: [...prev.messages, {
          id: -Date.now(),
          role: 'assistant',
          content,
          created_at: new Date().toISOString(),
        } as IChatMessage],
      }))
    } finally {
      patchSession(sid, { mindmapGenerating: false })
    }
  }, [patchSession, active.confirmedModuleName, active.confirmedModuleId])

  // 切到"生成前的知识库确认"阶段：先把面板挂出来（loading=true），
  // 再异步去后端拉 preview。失败时直接退化为"无知识注入"流程，给用户开始生成的入口。
  // 注意这是 phase='generate'；upload SSE 的 onKnowledgePreview 才是 phase='clarify'。
  const startKnowledgePreview = useCallback(async (
    sid: number,
    documentId: number | null,
    mindmapDocumentId: number | null,
    rounds: ClarificationRoundHistory[],
    moduleName: string,
    casePrefix: string,
  ) => {
    patchSession(sid, {
      followupActive: false,
      currentQuestions: null,
      knowledgePreview: {
        phase: 'generate',
        documentId, mindmapDocumentId, moduleName, casePrefix, rounds,
        loading: true, hits: [],
      },
    })
    // 注册取消器，让"停止任务"能中断这次预览检索请求。
    const controller = new AbortController()
    setCancel(sid, () => controller.abort())
    try {
      const preview = await fetchKnowledgePreview(sid, undefined, controller.signal)
      patchSession(sid, prev => (
        prev.knowledgePreview && prev.knowledgePreview.phase === 'generate'
          ? { knowledgePreview: { ...prev.knowledgePreview, loading: false, hits: preview.hits } }
          : {}
      ))
    } catch (err) {
      // 用户主动点"停止任务" → axios 抛 CanceledError：不算错误，只把 loading 收掉，
      // handleStopConfirm 的兜底已经把面板 loading 置 false，这里保持一致即可。
      const aborted =
        controller.signal.aborted
        || (err as { name?: string; code?: string })?.name === 'CanceledError'
        || (err as { code?: string })?.code === 'ERR_CANCELED'
      if (!aborted) console.error('Knowledge preview failed:', err)
      patchSession(sid, prev => (
        prev.knowledgePreview && prev.knowledgePreview.phase === 'generate'
          ? { knowledgePreview: { ...prev.knowledgePreview, loading: false, hits: [] } }
          : {}
      ))
    } finally {
      setCancel(sid, null)
    }
  }, [patchSession, setCancel])

  // clarify 阶段的确认：把用户勾选的知识 ids 传给 /api/clarify/initial/stream，
  // 走完整的 Clarifier 事件序列，落到 currentQuestions/currentSummary/currentRound。
  // 错误路径：把面板恢复成 loading=false 让用户重试，或直接进入"无澄清，直接预览生成"兜底。
  const startInitialClarification = useCallback((
    sid: number,
    documentId: number | null,
    mindmapDocumentId: number | null,
    knowledgeIds: number[] | null,
  ) => {
    // 把 knowledgePreview 关掉 + 切到一个"正在跑澄清"的 followupActive 视觉态（复用现有 amber loader）
    patchSession(sid, {
      knowledgePreview: null,
      followupActive: true,
      followupBuffer: '',
      currentQuestions: null,
    })

    const abort = streamInitialClarification(
      { sessionId: sid, documentId, mindmapDocumentId, knowledgeIds },
      {
        onStage: () => { /* surfaced via followupBuffer */ },
        onToken: (text) => {
          patchSession(sid, prev => ({ followupBuffer: prev.followupBuffer + text }))
        },
        onResult: (result) => {
          const qs = result.clarification?.questions ?? []
          const readyNow = result.clarification?.ready_to_generate || qs.length === 0
          // 替换 fakeUpload / 现有 uploadResult 为 Clarifier 跑出的真实 result。
          // 仅 PRD 模式：result 带 document_id + clarification → 落到 uploadResult；
          // 仅脑图模式：result 不带 PRD doc，但带 mindmap_document_id + clarification —— 仍落 uploadResult
          //   作为渲染 ClarificationPanel 的"哨兵"（filename/stats 字段对脑图模式无意义，但不会被消费）。
          patchSession(sid, prev => ({
            uploadResult: result.document_id != null
              ? result
              : (prev.uploadResult ?? {
                  ...result,
                  // 脑图独立模式：用 uploadMindmap 的 filename/stats 占位让 UploadResult 类型成立
                  document_id: null as unknown as number,  // ClarificationPanel 不读这个字段
                  filename: prev.uploadMindmap?.filename ?? result.filename,
                  stats: prev.uploadMindmap?.stats ?? result.stats,
                }),
            currentQuestions: readyNow ? null : qs,
            currentSummary: result.clarification?.summary ?? '',
            currentRound: 1,
            followupActive: false,
            followupBuffer: '',
          }))

          // 首轮澄清即"无遗留疑点/可直接生成"：与追问路径（handleClarificationConfirm）
          // 保持一致——不再停在空的澄清面板（那会渲染不出、导致流程死在这里），而是切到
          // 生成阶段的知识预览，让用户确认注入知识后生成用例。
          if (readyNow) {
            const cur = taskMapRef.current[sid]
            const prdDocId = result.document_id ?? (cur?.uploadResult?.document_id ?? documentId)
            const mmDocId = mindmapDocumentId ?? (cur?.uploadMindmap?.documentId ?? null)
            const moduleName =
              cur?.confirmedModuleName ?? result.clarification?.module_detected ?? ''
            const casePrefix =
              cur?.confirmedCasePrefix ?? result.clarification?.case_prefix_suggestion ?? ''
            void startKnowledgePreview(sid, prdDocId, mmDocId, cur?.clarificationRounds ?? [], moduleName, casePrefix)
          }
        },
        onAssistantMessage: (msg) => {
          patchSession(sid, prev => ({ messages: [...prev.messages, msg] }))
        },
        onError: (msg) => {
          console.error('Initial clarification error:', msg)
          patchSession(sid, { followupActive: false, followupBuffer: '' })
          setCancel(sid, null)
        },
        onDone: () => {
          patchSession(sid, { followupActive: false })
          setCancel(sid, null)
        },
      },
    )
    setCancel(sid, abort)
  }, [patchSession, setCancel, startKnowledgePreview])

  // 「开始生成」闸门：把本会话已暂存的 PRD / 脑图推进到下游流程。
  // 后端 /pipeline/start/stream 会跑模块自动分类 + 知识检索预览，发 module_auto_classified /
  // knowledge_preview 帧（复用上传阶段曾用的处理逻辑）；完成后再触发一次「PRD + 脑图」合并
  // 知识抽取（脑图优先），随后弹模块确认卡 / 知识审核 / 澄清面板。
  const handlePipelineStart = useCallback((sid: number) => {
    patchSession(sid, { pipelineStarting: true })
    const abort = streamPipelineStart(sid, {
      onStage: () => { /* 进度以 loading 呈现即可 */ },
      onToken: () => { /* pipeline/start 不产出正文 token */ },
      onResult: () => { /* pipeline/start 不发 result */ },
      onModuleAutoClassified: (payload) => handleModuleAutoClassified(sid, payload),
      onKnowledgePreview: (preview) => {
        patchSession(sid, prev => ({
          knowledgePreview: {
            phase: 'clarify',
            documentId: preview.document_id ?? prev.uploadResult?.document_id ?? null,
            mindmapDocumentId: preview.mindmap_document_id ?? prev.uploadMindmap?.documentId ?? null,
            moduleName: preview.module_name || null,
            casePrefix: null,
            loading: false,
            hits: preview.hits,
          },
        }))
      },
      onAssistantMessage: (msg) => {
        patchSession(sid, prev => ({ messages: [...prev.messages, msg] }))
      },
      onError: (msg) => {
        console.error('Pipeline start error:', msg)
        patchSession(sid, { pipelineStarting: false })
        setCancel(sid, null)
      },
      onDone: () => {
        patchSession(sid, { pipelineStarting: false })
        setCancel(sid, null)
        // 模块分类 / 知识预览已就绪 → 触发一次「PRD + 脑图」合并知识抽取（脑图优先）。
        // 抽取完成后弹草稿审核面板；草稿审核与模块确认卡都在知识预览面板之前的闸门里。
        void triggerCombinedExtraction(sid)
      },
    })
    setCancel(sid, abort)
  }, [patchSession, setCancel, handleModuleAutoClassified, triggerCombinedExtraction])

  const handleClarificationConfirm = useCallback(async (
    answers: Record<string, string>,
    moduleName: string,
    casePrefix: string,
  ) => {
    if (activeSessionId == null) return
    const sid = activeSessionId
    const cur = taskMap[sid]
    if (!cur || !cur.currentQuestions) return
    if (!cur.uploadResult && !cur.uploadMindmap) return

    const lockedModule = cur.confirmedModuleName ?? moduleName
    const lockedPrefix = cur.confirmedCasePrefix ?? casePrefix

    const newRound: ClarificationRoundHistory = {
      questions: cur.currentQuestions,
      answers: Object.fromEntries(
        cur.currentQuestions.map(q => [String(q.id), answers[String(q.id)] || '']),
      ),
    }
    const updatedRounds = [...cur.clarificationRounds, newRound]

    // PRD 文档 id 可能因为脑图独立模式 / startInitialClarification 拼出 fakeUpload 而为 null
    const prdDocId = (cur.uploadResult && (cur.uploadResult.document_id ?? null)) ?? null
    const mindmapDocId = cur.uploadMindmap?.documentId ?? null

    patchSession(sid, {
      clarificationRounds: updatedRounds,
      confirmedModuleName: lockedModule,
      confirmedCasePrefix: lockedPrefix,
    })

    // 已达上限：跳过追问直接进知识预览阶段
    if (updatedRounds.length >= MAX_ROUNDS) {
      await startKnowledgePreview(sid, prdDocId, mindmapDocId, updatedRounds, lockedModule, lockedPrefix)
      return
    }

    patchSession(sid, {
      followupActive: true,
      followupBuffer: '',
      currentQuestions: null,
    })

    const abort = streamFollowupClarification(
      {
        sessionId: sid,
        documentId: prdDocId,
        mindmapDocumentId: mindmapDocId,
        moduleName: lockedModule,
        casePrefix: lockedPrefix,
        rounds: updatedRounds,
      },
      {
        onStage: () => { /* surfaced via followupBuffer header */ },
        onToken: (text) => {
          patchSession(sid, prev => ({ followupBuffer: prev.followupBuffer + text }))
        },
        onResult: (res) => {
          const newQs = res.clarification.questions || []
          if (res.clarification.ready_to_generate || newQs.length === 0) {
            // 不再直接 runGenerate；切到知识预览面板，让用户确认要注入的条目
            startKnowledgePreview(sid, prdDocId, mindmapDocId, updatedRounds, lockedModule, lockedPrefix)
          } else {
            patchSession(sid, {
              currentQuestions: newQs,
              currentSummary: res.clarification.summary || '',
              currentRound: res.round,
            })
          }
        },
        onAssistantMessage: (msg) => {
          patchSession(sid, prev => ({ messages: [...prev.messages, msg] }))
        },
        onError: (msg) => {
          console.error('Follow-up error:', msg)
          setCancel(sid, null)
          // 追问出错也仍然给用户确认知识库的机会，避免静默直跑
          startKnowledgePreview(sid, prdDocId, mindmapDocId, updatedRounds, lockedModule, lockedPrefix)
        },
        onDone: () => {
          patchSession(sid, { followupActive: false })
          setCancel(sid, null)
        },
      },
    )
    setCancel(sid, abort)
  }, [activeSessionId, taskMap, patchSession, startKnowledgePreview, setCancel])

  // ── 脑图模式澄清 ───────────────────────────────────────────────────────────
  // 与用例模式共用后端澄清端点（/clarify/initial|followup/stream），它本就支持知识库命中注入
  // 与多轮追问。与用例版的唯一区别：澄清就绪后不进知识预览/生成用例，而是标记
  // mindmapReadyToGenerate 露出「生成测试脑图」卡。脑图模式上传的是 PRD → documentId=PRD id。

  // 脑图上传完成后自动触发首轮澄清（复用 followupActive amber loader 作"澄清中"可视态）。
  const startMindmapClarification = useCallback((sid: number, documentId: number) => {
    patchSession(sid, {
      followupActive: true,
      followupBuffer: '',
      currentQuestions: null,
      mindmapReadyToGenerate: false,
      clarificationRounds: [],
      currentRound: 1,
    })
    const abort = streamInitialClarification(
      { sessionId: sid, documentId, mindmapDocumentId: null, knowledgeIds: null },
      {
        onStage: () => { /* surfaced via followupBuffer */ },
        onToken: (text) => {
          patchSession(sid, prev => ({ followupBuffer: prev.followupBuffer + text }))
        },
        onResult: (result) => {
          const qs = result.clarification?.questions ?? []
          const readyNow = result.clarification?.ready_to_generate || qs.length === 0
          const detectedModule = result.clarification?.module_detected || ''
          patchSession(sid, prev => ({
            // 把 clarifier 识别到的模块名/摘要回填到 uploadResult 壳，供澄清面板做 suggestedModule
            uploadResult: prev.uploadResult
              ? { ...prev.uploadResult, clarification: {
                  ...prev.uploadResult.clarification,
                  module_detected: detectedModule,
                  summary: result.clarification?.summary ?? '',
                  questions: qs,
                  ready_to_generate: !!readyNow,
                } }
              : prev.uploadResult,
            currentQuestions: readyNow ? null : qs,
            currentSummary: result.clarification?.summary ?? '',
            currentRound: 1,
            followupActive: false,
            followupBuffer: '',
            // 首轮即无疑点 → 直接就绪，露出生成卡；否则等用户答完澄清面板
            mindmapReadyToGenerate: readyNow,
          }))
        },
        onAssistantMessage: (msg) => {
          patchSession(sid, prev => ({ messages: [...prev.messages, msg] }))
        },
        onError: (msg) => {
          console.error('Mindmap initial clarification error:', msg)
          // 澄清失败不该卡死流程：退化为"跳过澄清，允许直接生成"
          patchSession(sid, { followupActive: false, followupBuffer: '', mindmapReadyToGenerate: true })
          setCancel(sid, null)
        },
        onDone: () => {
          patchSession(sid, { followupActive: false })
          setCancel(sid, null)
        },
      },
    )
    setCancel(sid, abort)
  }, [patchSession, setCancel])

  // 桥接 ref：让前面定义的上传 onDone 能触发脑图首轮澄清。
  startMindmapClarificationRef.current = startMindmapClarification

  // 脑图澄清面板确认：并入本轮答案，未达上限且模型仍有追问则续问；否则标记就绪。
  const handleMindmapClarificationConfirm = useCallback((
    answers: Record<string, string>,
    moduleName: string,
    _casePrefix: string,  // 脑图不用用例编号前缀
  ) => {
    if (activeSessionId == null) return
    const sid = activeSessionId
    const cur = taskMapRef.current[sid]
    if (!cur || !cur.currentQuestions) return
    const prdDocId = cur.uploadResult?.document_id ?? null
    if (prdDocId == null) return

    const lockedModule = cur.confirmedModuleName ?? moduleName
    const newRound: ClarificationRoundHistory = {
      questions: cur.currentQuestions,
      answers: Object.fromEntries(
        cur.currentQuestions.map(q => [String(q.id), answers[String(q.id)] || '']),
      ),
    }
    const updatedRounds = [...cur.clarificationRounds, newRound]

    patchSession(sid, {
      clarificationRounds: updatedRounds,
      confirmedModuleName: lockedModule,
    })

    // 达轮次上限：不再追问，直接就绪允许生成
    if (updatedRounds.length >= MAX_ROUNDS) {
      patchSession(sid, { currentQuestions: null, mindmapReadyToGenerate: true })
      return
    }

    patchSession(sid, { followupActive: true, followupBuffer: '', currentQuestions: null })

    const abort = streamFollowupClarification(
      {
        sessionId: sid,
        documentId: prdDocId,
        mindmapDocumentId: null,
        moduleName: lockedModule,
        casePrefix: '',  // 脑图无前缀
        rounds: updatedRounds,
      },
      {
        onStage: () => { /* surfaced via followupBuffer */ },
        onToken: (text) => {
          patchSession(sid, prev => ({ followupBuffer: prev.followupBuffer + text }))
        },
        onResult: (res) => {
          const newQs = res.clarification.questions || []
          if (res.clarification.ready_to_generate || newQs.length === 0) {
            patchSession(sid, { currentQuestions: null, followupActive: false, mindmapReadyToGenerate: true })
          } else {
            patchSession(sid, {
              currentQuestions: newQs,
              currentSummary: res.clarification.summary || '',
              currentRound: res.round,
              followupActive: false,
            })
          }
        },
        onAssistantMessage: (msg) => {
          patchSession(sid, prev => ({ messages: [...prev.messages, msg] }))
        },
        onError: (msg) => {
          console.error('Mindmap follow-up error:', msg)
          setCancel(sid, null)
          // 追问出错也允许生成，避免静默卡住
          patchSession(sid, { followupActive: false, currentQuestions: null, mindmapReadyToGenerate: true })
        },
        onDone: () => {
          patchSession(sid, { followupActive: false })
          setCancel(sid, null)
        },
      },
    )
    setCancel(sid, abort)
  }, [activeSessionId, patchSession, setCancel])


  // role 区分 PRD / 脑图——决定动哪个 slot；acceptedIndices=null 等同"全部入库"，[] 等同"全部丢弃"。
  // 成功后把对应 slot 置 null；不主动推进下一步——澄清入口由 JSX gating 自然解锁。
  // acceptedDrafts=null → 全部丢弃；非空数组 → 按编辑后的内容入库（可能仅子集，可能 content/type 已被改写）
  // moduleChoice → 用户在面板里选的"加入哪个模块"（applyModule=true 时把入库/文档归属改到 moduleId）
  const settleDraftReview = useCallback(async (
    role: 'prd' | 'mindmap',
    acceptedDrafts: KnowledgeDraft[] | null,
    moduleChoice?: { applyModule: boolean; moduleId: number | null },
  ) => {
    if (activeSessionId == null) return
    const sid = activeSessionId
    const cur = taskMap[sid]
    if (!cur) return
    const slot = role === 'prd' ? cur.prdDraftReview : cur.mindmapDraftReview
    if (!slot || slot.submitting) return

    const slotKey: 'prdDraftReview' | 'mindmapDraftReview' =
      role === 'prd' ? 'prdDraftReview' : 'mindmapDraftReview'
    patchSession(sid, prev => {
      const cs = prev[slotKey]
      return cs ? { [slotKey]: { ...cs, submitting: true } } as Partial<SessionState> : {}
    })

    try {
      // null 走 acceptedIndices=[] 表示全部丢弃；否则把编辑后的草稿数组直接发给后端
      if (acceptedDrafts === null) {
        await confirmPendingKnowledge(slot.documentId, { acceptedIndices: [] }, moduleChoice)
      } else {
        await confirmPendingKnowledge(slot.documentId, { acceptedDrafts }, moduleChoice)
      }
      patchSession(sid, { [slotKey]: null } as Partial<SessionState>)
    } catch (err) {
      console.error('Confirm pending knowledge failed:', err)
      patchSession(sid, prev => {
        const cs = prev[slotKey]
        return cs ? { [slotKey]: { ...cs, submitting: false } } as Partial<SessionState> : {}
      })
    }
  }, [activeSessionId, taskMap, patchSession])

  const handleExport = useCallback(() => {
    if (!activeSessionId) return
    window.open(exportSessionUrl(activeSessionId), '_blank')
  }, [activeSessionId])

  // ── Stop running task ────────────────────────────────────────────────────
  // 拿当前正在跑的任务标签（中文，给确认弹窗用）。busy 优先级与渲染条件相同：
  // generating → followupActive → uploading → streaming → knowledgePreview.loading
  const runningTaskLabel = useMemo(() => {
    if (!active) return null
    if (active.generating) return '生成测试用例'
    if (active.followupActive) {
      return active.clarificationRounds.length === 0
        ? '识别澄清问题'
        : `第 ${active.clarificationRounds.length + 1} 轮澄清判断`
    }
    if (active.uploading) return '上传与解析文档'
    if (active.mindmapGenerating) return '生成测试脑图'
    if (active.pipelineStarting) return '分析模块与检索知识库'
    if (active.extractingDrafts) return '抽取产品知识'
    if (active.streaming) return '对话生成'
    if (active.prdDraftReview?.submitting || active.mindmapDraftReview?.submitting) return '提交知识草稿'
    if (active.knowledgePreview?.loading) return '加载知识库预览'
    return null
  }, [active])

  // 触发停止确认弹窗：只要当前会话有正在跑的任务就弹。
  // 注意：不能再要求「必须注册过 cancel fn」——像「加载知识库预览」这类路径
  // 没有可 abort 的 controller，但依然需要能停（靠 handleStopConfirm 的兜底清状态）。
  const handleStopRequest = useCallback(() => {
    if (activeSessionId == null) return
    if (!runningTaskLabel) return
    setStopConfirmSid(activeSessionId)
  }, [activeSessionId, runningTaskLabel])

  const handleStopConfirm = useCallback(() => {
    const sid = stopConfirmSid
    setStopConfirmSid(null)
    if (sid == null) return
    // cancel fn 是可选的：能 abort 的任务（streaming/upload/generate）有，
    // 「加载知识库预览」这类没有——后者只靠下面的兜底清状态解锁 UI。
    const fn = cancelMapRef.current.get(sid)
    if (fn) {
      cancelMapRef.current.delete(sid)
      try {
        fn()
      } catch (e) {
        console.error('Cancel current task failed:', e)
      }
    }
    // 兜底：如果 abort 走 catch 没及时清状态（e.g. axios 已经发完请求只在等响应），
    // 直接把所有 busy 标记一次性清掉，让 UI 立刻可用——后端的副作用 (已落库) 不会受影响。
    patchSession(sid, prev => ({
      streaming: prev.streaming ? false : prev.streaming,
      streamBuffer: prev.streaming ? '' : prev.streamBuffer,
      uploading: false,
      uploadStage: null,
      uploadProgress: '',
      followupActive: false,
      followupBuffer: '',
      generating: false,
      knowledgePreview: prev.knowledgePreview?.loading
        ? { ...prev.knowledgePreview, loading: false, hits: prev.knowledgePreview.hits ?? [] }
        : prev.knowledgePreview,
    }))
  }, [stopConfirmSid, patchSession])

  // ── Render ───────────────────────────────────────────────────────────────

  const showStreamBubble = active.streaming && active.streamBuffer

  return (
    <div className="flex h-full bg-gray-50 overflow-hidden">
      <TabBar value={view} onChange={onChangeView} />

      <aside className="w-64 bg-white border-r border-gray-200 flex-shrink-0">
        <SessionList
          sessions={sessions}
          activeId={activeSessionId}
          busyIds={busyIds}
          onSelect={handleSelectSession}
          onNew={(mode) => void handleCreateSession(mode)}
          onRename={async (id, title) => {
            const updated = await renameSession(id, title)
            setSessions(prev => prev.map(s => (s.id === id ? { ...s, title: updated.title } : s)))
          }}
          onDelete={handleDeleteSession}
        />
      </aside>

      <main className="flex-1 flex flex-col overflow-hidden">
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {!activeSessionId && (
            <div className="flex flex-col items-center justify-center h-full text-center text-gray-400">
              <FileText size={44} className="mb-4 opacity-30" />
              <p className="text-lg font-medium mb-1 text-gray-600">TestCraft AI</p>
              <p className="text-sm">从左侧选择「生成测试用例」或「生成测试脑图」开始</p>
            </div>
          )}

          {/* 步骤条：常驻在消息区顶部，让用户随时知道流程走到哪一步 */}
          {activeSessionId != null && (
            <div className="sticky top-0 z-10 -mx-6 px-6 py-2.5 bg-gray-50/95 backdrop-blur border-b border-gray-100">
              <FlowSteps mode={active.mode} currentStep={deriveFlowStep(active)} />
            </div>
          )}

          {/* 「开始」引导卡：会话已建、但还没上传任何资料、也没有任何产出时显示第一步入口。
              按钮直接可操作（触发隐藏 input / 飞书弹窗），把"提供文档"这一步直接嵌进引导流。 */}
          {activeSessionId != null && !active.uploading && !active.uploadResult && !active.uploadMindmap
            && active.testCases.length === 0 && !active.mindmapResult && active.messages.length === 0 && (
            <div className={`rounded-xl border p-4 ${active.mode === 'mindmap' ? 'bg-indigo-50/60 border-indigo-200' : 'bg-blue-50/60 border-blue-200'}`}>
              <div className="flex items-center gap-2 mb-1.5">
                {active.mode === 'mindmap'
                  ? <Brain size={16} className="text-indigo-600" />
                  : <FileText size={16} className="text-blue-600" />}
                <span className="text-sm font-medium text-gray-800">
                  {active.mode === 'mindmap' ? '第一步：上传需求文档' : '第一步：上传资料'}
                </span>
              </div>
              <p className="text-xs text-gray-600 leading-relaxed mb-3">
                {active.mode === 'mindmap'
                  ? '上传需求文档（本地 .docx/.pdf 或飞书链接），系统会先结合知识库澄清疑点，确认后生成测试脑图并存入飞书。'
                  : '上传需求文档和/或测试脑图（本地文件或飞书链接，可只传其一），上传完成后按引导继续。'}
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => guideFileRef.current?.click()}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-white bg-blue-600 rounded-full hover:bg-blue-700 transition-colors"
                >
                  <Upload size={15} /> 上传需求文档
                </button>
                {active.mode !== 'mindmap' && (
                  <>
                    <button
                      type="button"
                      onClick={() => guideMindmapRef.current?.click()}
                      className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-700 bg-white border border-gray-200 rounded-full hover:border-emerald-300 hover:text-emerald-600 transition-colors"
                    >
                      <Network size={15} /> 上传测试脑图
                    </button>
                    <button
                      type="button"
                      onClick={() => setPasteDialogOpen(true)}
                      className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-700 bg-white border border-gray-200 rounded-full hover:border-emerald-300 hover:text-emerald-600 transition-colors"
                    >
                      <Pencil size={15} /> 粘贴脑图大纲
                    </button>
                  </>
                )}
                <button
                  type="button"
                  onClick={() => setLarkDialogOpen(true)}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-700 bg-white border border-gray-200 rounded-full hover:border-amber-300 hover:text-amber-600 transition-colors"
                >
                  <LinkIcon size={15} /> 飞书链接
                </button>
              </div>
            </div>
          )}

          {active.messages.map(msg => (
            <ChatMessage key={msg.id} role={msg.role} content={msg.content} />
          ))}

          {showStreamBubble && (
            <ChatMessage role="assistant" content={active.streamBuffer} isStreaming />
          )}

          {/* 已上传 PRD / 脑图 chips —— 让用户随时感知本会话两个 slot 各自的状态。
              附带正文总字符数 + 是否超过预览截断阈值（与后端 DEFAULT_DOC_LIMIT=30000 一致）。 */}
          {!active.uploading && !active.generating && (active.uploadResult || active.uploadMindmap) && (
            <div className="flex flex-wrap gap-2">
              {active.uploadResult && active.uploadResult.filename && (() => {
                const len = active.uploadResult.stats?.raw_text_length ?? 0
                const truncated = len > DOC_PREVIEW_LIMIT
                return (
                  <span className="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-blue-50 border border-blue-200 text-xs text-blue-700">
                    <FileText size={12} />
                    PRD：{active.uploadResult.filename}
                    {len > 0 && (
                      <span className="text-blue-500/80">· {len.toLocaleString()} 字</span>
                    )}
                    {truncated && (
                      <span
                        className="px-1 py-0.5 rounded bg-amber-100 text-amber-700 border border-amber-200"
                        title={`正文超过预览上限（${DOC_PREVIEW_LIMIT.toLocaleString()} 字），喂给大模型时会保留开头与结尾、省略中间部分`}
                      >
                        已截断
                      </span>
                    )}
                  </span>
                )
              })()}
              {active.uploadMindmap && (() => {
                const len = active.uploadMindmap.stats?.raw_text_length ?? 0
                const truncated = len > DOC_PREVIEW_LIMIT
                return (
                  <span className="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-emerald-50 border border-emerald-200 text-xs text-emerald-700">
                    <FileText size={12} />
                    脑图：{active.uploadMindmap.filename}
                    {len > 0 && (
                      <span className="text-emerald-600/80">· {len.toLocaleString()} 字</span>
                    )}
                    {truncated && (
                      <span
                        className="px-1 py-0.5 rounded bg-amber-100 text-amber-700 border border-amber-200"
                        title={`正文超过预览上限（${DOC_PREVIEW_LIMIT.toLocaleString()} 字），喂给大模型时会保留开头与结尾、省略中间部分`}
                      >
                        已截断
                      </span>
                    )}
                  </span>
                )
              })()}
            </div>
          )}

          {/* 「开始生成」闸门：上传只暂存文档，用户备齐资料后点它才进入下游流程
              （模块归类 → 知识审核 → 澄清 → 生成用例）。只在"有暂存文档、且下游流程尚未启动"时显示。 */}
          {active.mode !== 'mindmap' && (active.uploadResult || active.uploadMindmap)
            && !active.uploading && !active.generating && !active.pipelineStarting
            && !active.extractingDrafts
            && !active.moduleDecision && !active.prdDraftReview && !active.mindmapDraftReview
            && !active.knowledgePreview
            && !(active.currentQuestions && active.currentQuestions.length > 0)
            && !active.followupActive && !active.pendingGenerate
            && active.testCases.length === 0 && (
            <div className="rounded-xl border border-blue-200 bg-blue-50/60 p-4">
              <p className="text-sm font-medium text-gray-800 mb-1">资料已就绪</p>
              <p className="text-xs text-gray-600 leading-relaxed mb-3">
                已暂存
                {active.uploadResult?.filename ? ` PRD《${active.uploadResult.filename}》` : ''}
                {active.uploadResult && active.uploadMindmap ? ' 与' : ''}
                {active.uploadMindmap ? ` 测试脑图《${active.uploadMindmap.filename}》` : ''}
                。可继续上传其它资料，准备好后点「开始生成」进入模块归类、知识审核与澄清流程。
              </p>
              <button
                type="button"
                onClick={() => { if (activeSessionId != null) handlePipelineStart(activeSessionId) }}
                className="inline-flex items-center gap-1.5 px-4 py-2 text-sm text-white bg-blue-600 rounded-full hover:bg-blue-700 transition-colors"
              >
                开始生成
              </button>
            </div>
          )}

          {active.mode !== 'mindmap' && (active.uploadResult || active.uploadMindmap) && active.currentQuestions && active.currentQuestions.length > 0 && !active.generating && !active.followupActive && !active.extractingDrafts && !active.prdDraftReview && !active.mindmapDraftReview && !active.moduleDecision && (
            <ClarificationPanel
              questions={active.currentQuestions}
              summary={active.currentSummary || active.uploadResult?.clarification?.summary || ''}
              suggestedModule={active.uploadResult?.clarification?.module_detected || ''}
              suggestedPrefix={active.uploadResult?.clarification?.case_prefix_suggestion}
              round={active.currentRound}
              maxRounds={MAX_ROUNDS}
              lockedModuleName={active.confirmedModuleName ?? undefined}
              lockedCasePrefix={active.confirmedCasePrefix ?? undefined}
              confirmLabel={
                active.currentRound >= MAX_ROUNDS
                  ? '提交回答并生成测试用例'
                  : '提交回答，让大模型判断是否还需要继续澄清'
              }
              onConfirm={handleClarificationConfirm}
            />
          )}

          {/* 第 1 步：模块确认卡。LLM 判定文档归属后（含高置信自动命中）一律让用户拍板。
              未处理时阻塞下方知识草稿审核与知识预览，保证「模块 → 知识草稿 → 开始澄清」的顺序。 */}
          {active.mode !== 'mindmap' && active.moduleDecision && activeSessionId != null && (
            <ModuleConfirmPanel
              modules={modules}
              suggestedModuleId={active.moduleDecision.suggestedModuleId}
              suggestedModuleName={active.moduleDecision.suggestedModuleName}
              applied={active.moduleDecision.applied}
              confidence={active.moduleDecision.confidence}
              reasoning={active.moduleDecision.reasoning}
              selectedModuleId={active.moduleDecision.selectedModuleId}
              createNew={active.moduleDecision.createNew}
              createName={active.moduleDecision.createName}
              createCode={active.moduleDecision.createCode}
              createDescription={active.moduleDecision.createDescription}
              creating={active.moduleDecision.creating}
              onPatch={(patch) => patchModuleDecision(activeSessionId, patch)}
              onConfirm={() => confirmModuleDecision(activeSessionId, active.moduleDecision!)}
              onDismiss={() => dismissModuleDecision(activeSessionId)}
            />
          )}

          {/* 第 2 步：知识草稿审核闸门——上传后 LLM 抽取出的产品规则/约束/术语先让用户勾选要入库的条目。
              两个 slot 独立呈现；moduleDecision 未处理时不渲染（先走模块确认）。 */}
          {/* 合并抽取进行中：显示 loader，占住审核面板位置，抽完再落草稿/进预览 */}
          {active.pipelineStarting && (
            <div className="bg-blue-50/60 border border-blue-200 rounded-xl p-3">
              <div className="flex items-center gap-2 text-sm text-blue-700">
                <Loader2 size={14} className="animate-spin" />
                正在分析文档归属模块并检索项目知识库…
              </div>
            </div>
          )}

          {active.extractingDrafts && !active.moduleDecision && !active.prdDraftReview && !active.mindmapDraftReview && (
            <div className="bg-emerald-50/60 border border-emerald-200 rounded-xl p-3">
              <div className="flex items-center gap-2 text-sm text-emerald-700">
                <Loader2 size={14} className="animate-spin" />
                {active.uploadResult && active.uploadMindmap
                  ? '正在合并需求文档与脑图，抽取可沉淀的产品知识（冲突以脑图为准）…'
                  : '正在抽取可沉淀的产品知识…'}
              </div>
            </div>
          )}

          {active.mode !== 'mindmap' && active.prdDraftReview && !active.moduleDecision && (
            <KnowledgeDraftReviewPanel
              documentId={active.prdDraftReview.documentId}
              role="prd"
              combined={!!active.uploadMindmap}
              filename={active.prdDraftReview.filename}
              moduleName={active.prdDraftReview.moduleName}
              modules={modules}
              defaultModuleId={draftDefaultModuleId}
              drafts={active.prdDraftReview.drafts}
              submitting={active.prdDraftReview.submitting}
              onConfirm={(drafts, moduleChoice) => settleDraftReview('prd', drafts, moduleChoice)}
              onDiscard={() => settleDraftReview('prd', null)}
            />
          )}
          {active.mode !== 'mindmap' && active.mindmapDraftReview && !active.moduleDecision && (
            <KnowledgeDraftReviewPanel
              documentId={active.mindmapDraftReview.documentId}
              role="mindmap"
              filename={active.mindmapDraftReview.filename}
              moduleName={active.mindmapDraftReview.moduleName}
              modules={modules}
              defaultModuleId={draftDefaultModuleId}
              drafts={active.mindmapDraftReview.drafts}
              submitting={active.mindmapDraftReview.submitting}
              onConfirm={(drafts, moduleChoice) => settleDraftReview('mindmap', drafts, moduleChoice)}
              onDiscard={() => settleDraftReview('mindmap', null)}
            />
          )}

          {active.followupActive && !active.prdDraftReview && !active.mindmapDraftReview && (
            <div className="bg-amber-50/60 border border-amber-200 rounded-xl p-3 space-y-2">
              <div className="flex items-center gap-2 text-sm text-amber-700">
                <Loader2 size={14} className="animate-spin" />
                {active.clarificationRounds.length === 0
                  ? '正在结合本文档与项目知识库识别澄清问题…'
                  : `正在结合您第 ${active.clarificationRounds.length} 轮的回答判断是否还需要继续澄清…`}
              </div>
              {active.followupBuffer && (
                <pre className="text-xs text-gray-600 whitespace-pre-wrap font-mono leading-relaxed max-h-72 overflow-auto">
                  {active.followupBuffer}
                </pre>
              )}
            </div>
          )}

          {/* 脑图模式澄清面板：与用例模式共用组件，隐藏用例编号前缀、终点是生成脑图。 */}
          {active.mode === 'mindmap' && active.uploadResult && active.currentQuestions
            && active.currentQuestions.length > 0 && !active.followupActive
            && !active.mindmapGenerating && (
            <ClarificationPanel
              questions={active.currentQuestions}
              summary={active.currentSummary}
              suggestedModule={active.confirmedModuleName || active.uploadResult?.clarification?.module_detected || ''}
              hideCasePrefix
              round={active.currentRound}
              maxRounds={MAX_ROUNDS}
              lockedModuleName={active.confirmedModuleName ?? undefined}
              confirmLabel={
                active.currentRound >= MAX_ROUNDS
                  ? '提交回答并生成测试脑图'
                  : '提交回答，让大模型判断是否还需要继续澄清'
              }
              onConfirm={handleMindmapClarificationConfirm}
            />
          )}

          {/* 脑图模式主操作卡：澄清就绪后出现，点按钮生成脑图并存飞书。仅脑图模式显示。 */}
          {active.mode === 'mindmap' && active.uploadResult?.document_id != null
            && active.mindmapReadyToGenerate && (
            <div className="bg-indigo-50/60 border border-indigo-200 rounded-xl p-4 space-y-2">
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2 text-sm text-indigo-900">
                  <Brain size={16} className="text-indigo-600" />
                  <span>
                    {active.mindmapResult
                      ? '测试脑图已生成并存入飞书'
                      : `已完成澄清${active.uploadResult.filename ? `《${active.uploadResult.filename}》` : ''}，点击生成测试脑图`}
                  </span>
                </div>
                <button
                  type="button"
                  onClick={() => void runGenerateMindmap(activeSessionId!, active.uploadResult!.document_id!)}
                  disabled={active.mindmapGenerating}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-indigo-600 rounded-full hover:bg-indigo-700 disabled:opacity-50 whitespace-nowrap"
                >
                  {active.mindmapGenerating
                    ? <><Loader2 size={13} className="animate-spin" /> 生成中…</>
                    : <><Brain size={13} /> {active.mindmapResult ? '重新生成' : '生成测试脑图'}</>}
                </button>
              </div>
              {active.mindmapResult && (
                <a
                  href={active.mindmapResult.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 text-xs font-medium text-indigo-700 hover:text-indigo-900 underline"
                >
                  {active.mindmapResult.title} <ExternalLink size={12} />
                </a>
              )}
            </div>
          )}

          {/* 上传 SSE 里 knowledge_preview 帧先于产品知识抽取（knowledge_extract 阶段）到达，
              但抽取仍在跑（uploading 尚未 done）。此时不渲染预览面板，避免"开始澄清"按钮
              抢在抽取完成/草稿审核之前出现。抽取完成后 onDone 置 uploading=false，面板才亮。
              phase='generate' 的预览由 startKnowledgePreview 触发（uploading 早已 false），不受影响。 */}
          {active.mode !== 'mindmap' && active.knowledgePreview && !active.uploading && !active.extractingDrafts && !active.generating && !active.prdDraftReview && !active.mindmapDraftReview && !active.moduleDecision && (
            <KnowledgePreviewPanel
              loading={active.knowledgePreview.loading}
              hits={active.knowledgePreview.hits}
              moduleName={active.knowledgePreview.moduleName}
              casePrefix={active.knowledgePreview.casePrefix}
              phase={active.knowledgePreview.phase}
              onConfirm={(selectedIds) => {
                const kp = active.knowledgePreview!
                if (kp.phase === 'clarify') {
                  startInitialClarification(activeSessionId!, kp.documentId, kp.mindmapDocumentId, selectedIds)
                } else {
                  runGenerate(
                    activeSessionId!, kp.documentId, kp.mindmapDocumentId, kp.rounds,
                    kp.moduleName, kp.casePrefix, selectedIds,
                  )
                }
              }}
            />
          )}

          {active.mode !== 'mindmap' && (active.generating || (active.pendingGenerate && active.testCases.length === 0)) && (
            <div className="bg-blue-50/70 border border-blue-200 rounded-xl p-4 flex items-center gap-3">
              <Loader2 size={18} className="animate-spin text-blue-600" />
              <div className="text-sm text-blue-800">
                正在生成测试用例
              </div>
            </div>
          )}

          {active.mode !== 'mindmap' && active.testCases.length > 0 && (
            <TestCaseTable
              cases={active.testCases}
              onExport={handleExport}
              onCaseUpdate={(id, patch) =>
                patchSession(activeSessionId!, prev => ({
                  testCases: prev.testCases.map(c => (c.id === id ? { ...c, ...patch } : c)),
                }))
              }
              onCaseDelete={(id) =>
                patchSession(activeSessionId!, prev => ({
                  testCases: prev.testCases.filter(c => c.id !== id),
                }))
              }
            />
          )}

          {active.uploading && (
            <div className="bg-blue-50/60 border border-blue-200 rounded-xl p-3 space-y-2">
              <div className="flex items-center gap-2 text-sm text-blue-700">
                <Loader2 size={14} className="animate-spin" />
                {active.uploadStage === 'starting' && '准备上传…'}
                {active.uploadStage === 'validating' && '校验飞书链接…'}
                {active.uploadStage === 'fetching' && '通过 lark-cli 抓取飞书文档…'}
                {active.uploadStage === 'fingerprinting' && '计算文档指纹…'}
                {active.uploadStage === 'cache_hit' && '命中缓存，复用上次结果'}
                {active.uploadStage === 'parsing' && '解析文档中…'}
                {active.uploadStage === 'parsed' && '解析完成'}
                {active.uploadStage === 'persisted' && '入库完成'}
                {active.uploadStage === 'clarifying' && '大模型识别歧义中…'}
              </div>
              {active.uploadProgress && (
                <pre className="text-xs text-gray-600 whitespace-pre-wrap font-mono leading-relaxed max-h-72 overflow-auto">
                  {active.uploadProgress}
                </pre>
              )}
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        <div className="px-6 py-4 border-t border-gray-200 bg-white space-y-2">
          {runningTaskLabel && (
            <div className="flex items-center justify-between gap-3 px-3 py-2 rounded-lg bg-amber-50 border border-amber-200">
              <div className="flex items-center gap-2 text-sm text-amber-800">
                <Loader2 size={14} className="animate-spin" />
                <span>正在执行：{runningTaskLabel}</span>
              </div>
              <button
                onClick={handleStopRequest}
                type="button"
                className="flex items-center gap-1 px-3 py-1 text-xs text-red-600 border border-red-200 rounded-full hover:bg-red-50 transition-colors"
              >
                <Square size={12} fill="currentColor" />
                停止任务
              </button>
            </div>
          )}
          <MessageInput
            value={input}
            onChange={setInput}
            onSend={handleSend}
            onFileSelect={handleFileSelect}
            onMindmapSelect={active.mode === 'mindmap' ? undefined : handleMindmapSelect}
            onMindmapPaste={active.mode === 'mindmap' ? undefined : () => setPasteDialogOpen(true)}
            onLarkImport={() => setLarkDialogOpen(true)}
            placeholder={active.mode === 'mindmap'
              ? '上传需求文档后即可生成测试脑图…（也可输入消息）'
              : undefined}
            disabled={active.streaming || active.uploading || active.generating || active.followupActive || active.knowledgePreview != null || active.prdDraftReview != null || active.mindmapDraftReview != null}
          />
        </div>
      </main>

      {/* 引导卡上传按钮的隐藏 input（与 MessageInput 内的等价，供第一步引导流直接触发） */}
      <input
        ref={guideFileRef}
        type="file"
        accept=".docx,.pdf"
        className="hidden"
        onChange={e => {
          const f = e.target.files?.[0]
          if (f) void handleFileSelect(f)
          e.target.value = ''
        }}
      />
      <input
        ref={guideMindmapRef}
        type="file"
        accept=".md,.markdown,text/markdown"
        className="hidden"
        onChange={e => {
          const f = e.target.files?.[0]
          if (f) void handleMindmapSelect(f)
          e.target.value = ''
        }}
      />

      <LarkUrlDialog
        open={larkDialogOpen}
        onClose={() => setLarkDialogOpen(false)}
        onSubmit={handleLarkImport}
        loading={active.uploading}
        mindmapOnly={active.mode === 'mindmap'}
      />

      <MindmapPasteDialog
        open={pasteDialogOpen}
        onClose={() => setPasteDialogOpen(false)}
        onSubmit={handleMindmapPaste}
        loading={active.uploading}
      />

      <StopConfirmDialog
        open={stopConfirmSid != null}
        taskLabel={runningTaskLabel ?? '当前任务'}
        onClose={() => setStopConfirmSid(null)}
        onConfirm={handleStopConfirm}
      />
    </div>
  )
}
