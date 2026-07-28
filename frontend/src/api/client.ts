import axios from 'axios'

const BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001'

export const api = axios.create({
  baseURL: BASE_URL,
  headers: { 'Content-Type': 'application/json' },
})

// ── Auth + project context (module-local, attached to every request) ─────────

const TOKEN_KEY = 'caseweave.token'
const PROJECT_KEY = 'caseweave.project_id'

let _token: string | null = localStorage.getItem(TOKEN_KEY)
let _projectId: number | null = (() => {
  const raw = localStorage.getItem(PROJECT_KEY)
  return raw ? Number(raw) || null : null
})()

export const getToken = () => _token
export const getProjectId = () => _projectId

export function setToken(token: string | null) {
  _token = token
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export function setProjectId(projectId: number | null) {
  _projectId = projectId
  if (projectId != null) localStorage.setItem(PROJECT_KEY, String(projectId))
  else localStorage.removeItem(PROJECT_KEY)
}

// Helpers to build headers consistently for both axios + fetch.
function authHeaders(): Record<string, string> {
  const h: Record<string, string> = {}
  if (_token) h.Authorization = `Bearer ${_token}`
  if (_projectId != null) h['X-Project-Id'] = String(_projectId)
  return h
}

api.interceptors.request.use(cfg => {
  const extra = authHeaders()
  cfg.headers = { ...(cfg.headers || {}), ...extra } as typeof cfg.headers
  return cfg
})

// Centralized 401 handler — purge token + project, let UI re-render on next render tick.
let _onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(fn: (() => void) | null) {
  _onUnauthorized = fn
}

api.interceptors.response.use(
  r => r,
  err => {
    if (err?.response?.status === 401) {
      setToken(null)
      setProjectId(null)
      _onUnauthorized?.()
    }
    return Promise.reject(err)
  },
)

// ── Auth API ─────────────────────────────────────────────────────────────────

export interface AuthUser {
  id: number
  email: string
  name: string
  is_admin: boolean
}

export interface AuthResponse {
  token: string
  user: AuthUser
}

export const register = (email: string, password: string, name?: string) =>
  api.post<AuthResponse>('/api/auth/register', { email, password, name }).then(r => r.data)

export const login = (email: string, password: string) =>
  api.post<AuthResponse>('/api/auth/login', { email, password }).then(r => r.data)

export const fetchMe = () =>
  api.get<AuthUser>('/api/auth/me').then(r => r.data)

// ── Projects ─────────────────────────────────────────────────────────────────

export interface Project {
  id: number
  name: string
  description: string | null
  creator_id: number | null
  is_public: boolean
  created_at: string | null
}

export const fetchProjects = () =>
  api.get<Project[]>('/api/projects').then(r => r.data)

export const createProject = (name: string, description?: string, isPublic = false) =>
  api.post<Project>('/api/projects', { name, description, is_public: isPublic }).then(r => r.data)

export const updateProject = (
  id: number,
  patch: { name?: string; description?: string; is_public?: boolean },
) => api.patch<Project>(`/api/projects/${id}`, patch).then(r => r.data)

export const deleteProject = (id: number) =>
  api.delete(`/api/projects/${id}`).then(r => r.data)


export interface ChatSession {
  id: number
  title: string
  mode?: 'cases' | 'mindmap'  // 会话用途：生成用例 / 生成测试脑图（老会话无值按 cases）
  status: string
  created_at: string
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  meta?: Record<string, unknown> | null
  created_at: string
}

export interface TestCase {
  id: number
  case_number: string
  name: string
  module: string
  priority: 'P1' | 'P2' | 'P3'
  preconditions: string
  steps: string
  expected_result: string
  remarks: string
}

export interface ClarificationQuestion {
  id: number
  category: string
  question: string
  context: string
  importance: 'high' | 'medium' | 'low'
  // LLM 给出的候选答案，前端单选；用户也可选「自定义」自己填
  options?: string[]
}

export interface UploadResult {
  document_id: number | null
  // 当用户走 /upload/mindmap/stream 上传脑图时，document_id 可能为 null，由
  // mindmap_document_id 承载；/clarify/initial/stream 的结果帧也带这两个字段。
  mindmap_document_id?: number | null
  filename: string
  stats: { chunks: number; tables: number; raw_text_length: number }
  // PRD-only / 飞书路径会带 clarification；脑图独立 result 帧不带 clarification（脑图只算预览阶段）
  clarification?: {
    summary: string
    module_detected: string
    case_prefix_suggestion?: string
    questions: ClarificationQuestion[]
    ready_to_generate: boolean
  }
}

// ── Sessions ──────────────────────────────────────────────────────────────────

export const fetchSessions = () =>
  api.get<ChatSession[]>('/api/sessions').then(r => r.data)

export const createSession = (title: string, module_id?: number, mode?: 'cases' | 'mindmap') =>
  api.post<ChatSession>('/api/sessions', { title, module_id, mode }).then(r => r.data)

export const renameSession = (sessionId: number, title: string) =>
  api.patch<ChatSession>(`/api/sessions/${sessionId}`, { title }).then(r => r.data)

export const deleteSession = (sessionId: number) =>
  api.delete<{ deleted: boolean; id: number }>(`/api/sessions/${sessionId}`).then(r => r.data)

export const fetchMessages = (sessionId: number) =>
  api.get<ChatMessage[]>(`/api/sessions/${sessionId}/messages`).then(r => r.data)

// 澄清运行态——会话进入时拉一次复原 SessionState；状态尚未建立返回 null
export interface ClarificationStateDTO {
  session_id: number
  document_id: number | null
  mindmap_document_id: number | null
  prd_filename: string | null
  prd_stats: { chunks: number; tables: number; raw_text_length: number } | null
  mindmap_filename: string | null
  mindmap_stats: { chunks: number; tables: number; raw_text_length: number } | null
  // 抽取出尚未审核确认入库的知识草稿；null 表示已 settle（无草稿待审），
  // [] 表示抽取完成但没产出条目（前端按"无草稿"处理）。
  prd_pending_drafts: KnowledgeDraft[] | null
  mindmap_pending_drafts: KnowledgeDraft[] | null
  // 「建议新建模块」提议——刷新后从已落库的 module_auto_classified 气泡复原；
  // 引用文档仍未归类且当时未自动落库时才有值，否则为 null。
  module_proposal: {
    document_id: number | null
    name: string
    code: string
    description: string | null
  } | null
  summary: string | null
  module_detected: string | null
  case_prefix_suggestion: string | null
  confirmed_module_name: string | null
  confirmed_case_prefix: string | null
  current_round: number
  rounds: ClarificationRoundHistory[]
  current_questions: ClarificationQuestion[]
  ready_to_generate: boolean
  status: 'clarifying' | 'staged' | 'awaiting_clarification' | 'awaiting_answers' | 'generating' | 'done' | 'error'
  updated_at: string | null
}

export const fetchClarificationState = async (sessionId: number): Promise<ClarificationStateDTO | null> => {
  try {
    const r = await api.get<ClarificationStateDTO>(`/api/sessions/${sessionId}/clarification_state`)
    return r.data
  } catch (e: unknown) {
    const status = (e as { response?: { status?: number } })?.response?.status
    if (status === 404) return null
    throw e
  }
}

// ── Upload ────────────────────────────────────────────────────────────────────

export const uploadDocument = (file: File, moduleId?: number) => {
  const form = new FormData()
  form.append('file', file)
  if (moduleId !== undefined) form.append('module_id', String(moduleId))
  return api.post<UploadResult>('/api/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  }).then(r => r.data)
}

export interface UploadStreamCallbacks {
  onStage: (stage: string, message: string) => void
  onToken: (text: string) => void
  onResult: (result: UploadResult) => void
  onAssistantMessage?: (msg: ChatMessage) => void
  // B 方案：upload/lark stream 跑到"文档已 persist 但 Clarifier 还没跑"时会发一帧
  // knowledge_preview，里面是即将注入到 Clarifier 提示词里的候选条目，让用户先勾选。
  // 用户在前端确认后再调 streamInitialClarification 触发真正的澄清。
  onKnowledgePreview?: (preview: UploadKnowledgePreview) => void
  // 新流程：上传只「暂存」文档（解析入库，不跑任何大模型），完成时发一帧 staged。
  // 前端据此展示"已上传"chip + 「开始生成」按钮，用户备齐资料后点按钮才进入下游流程。
  onStaged?: (payload: StagedPayload) => void
  // 抽取出的知识草稿（用户确认后才入库）。drafts 可能为 []（抽完了但 LLM 没产出条目），
  // 前端用空数组判定"直接进入澄清"；非空时需要先在 UI 弹审核面板。
  onKnowledgeDrafts?: (payload: ExtractedKnowledgeDrafts) => void
  // 用户没指定模块、后端 LLM 自动归类：高置信度直接 applied=true（已落库）；
  // 中等置信度 applied=false，前端弹"建议归类到 XX，是否采用？"
  onModuleAutoClassified?: (payload: ModuleAutoClassifiedPayload) => void
  onError: (message: string) => void
  onDone: () => void
}

export interface ModuleAutoClassifiedPayload {
  document_id?: number
  module_id: number | null
  module_name: string | null
  // 都不匹配已有模块时，LLM 提议新建的模块（前端弹确认卡，由用户决定是否创建）
  proposed_module?: { name: string; code: string | null; description: string | null } | null
  suggestion: {
    module_id: number | null
    confidence: number
    reasoning: string
    applied: boolean
  }
}

export interface KnowledgeConflictHint {
  entry_id: number
  knowledge_type: string
  content: string
  confidence: number
  distance: number
  // LLM 判定的关系：similar（相似，可共存）/ conflict（冲突，取值矛盾）
  relation?: 'similar' | 'conflict'
  // 判定理由（一句中文），展示给用户复核
  reason?: string
}

export interface KnowledgeDraft {
  knowledge_type: string
  content: string
  source: string
  confidence: number
  // 本草稿与库内已有条目的整体关系：new（无冲突）/ similar / conflict
  relation_status?: 'new' | 'similar' | 'conflict'
  // 可选：本草稿与已有库内条目语义相近时的"相似 / 冲突"近邻
  conflicts?: KnowledgeConflictHint[]
  // 用户在审核面板选"保留新的"时，带上被替代的旧条目 id；入库时后端会删除它们。
  supersedes_entry_ids?: number[]
}

export interface ExtractedKnowledgeDrafts {
  document_id: number
  module_id: number | null
  module_name: string | null
  role: 'prd' | 'mindmap'
  source?: string
  drafts: KnowledgeDraft[]
  // "ok" 正常（含抽到空）；"error" 表示知识抽取 LLM 调用超时/报错被跳过，
  // 前端据此给用户提示（不影响后续澄清 / 生成）。仅 extract_combined_drafts 返回。
  extract_status?: 'ok' | 'error'
}

// 上传完成后触发的「PRD + 脑图」合并知识抽取（脑图优先）。
// 后端读本会话的 PRD/脑图文档合成一次抽取，产物落到主文档并清空副文档，返回草稿。
// document_id 可能为 null（会话还没任何文档）→ drafts 为空。
export const extractCombinedDrafts = (sessionId: number) =>
  api.post<ExtractedKnowledgeDrafts & { document_id: number | null }>(
    '/api/documents/extract_combined_drafts', { session_id: sessionId },
    // 后端单次 LLM 调用超时 120s、最多重试 1 次（最坏 ~240s）。前端超时给足余量，
    // 避免网络/后端偶发挂起时「正在抽取产品知识…」loader 永久打转（catch 分支会清 loader）。
    { timeout: 300_000 },
  ).then(r => r.data)

export interface UploadKnowledgePreview {
  document_id: number
  // pipeline/start 阶段会带上会话里并存的另一份文档 id（PRD + 脑图合并预览）
  mindmap_document_id?: number | null
  module_id: number | null
  filename: string
  module_name: string | null
  stats: { chunks: number; tables: number; raw_text_length: number }
  hits: KnowledgeHit[]
  // 飞书路径还会带 source: "lark" + url，文件路径不带
  source?: string
  url?: string
  // 脑图路径带 role: "mindmap"，前端用它区分 UI 行为（不展示"开始澄清"按钮等）
  role?: 'prd' | 'mindmap'
  cached?: boolean
}

// 上传「暂存」完成帧：文档已解析入库，尚未跑任何大模型。
export interface StagedPayload {
  document_id: number
  role: 'prd' | 'mindmap'
  filename: string
  module_id: number | null
  stats: { chunks: number; tables: number; raw_text_length: number }
  source?: string
  url?: string
  cached?: boolean
}
export function streamUpload(
  file: File,
  sessionId: number,
  callbacks: UploadStreamCallbacks,
  moduleId?: number,
): () => void {
  const form = new FormData()
  form.append('file', file)
  form.append('session_id', String(sessionId))
  if (moduleId !== undefined) form.append('module_id', String(moduleId))

  const controller = new AbortController()

  fetch(`${BASE_URL}/api/upload/stream`, {
    method: 'POST',
    body: form,
    headers: authHeaders(),
    signal: controller.signal,
  }).then(async res => {
    if (!res.ok || !res.body) {
      callbacks.onError(`HTTP ${res.status}`)
      return
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let sawTerminal = false  // 是否收到过 done/error 终止帧；用于识别"连接被中途掐断"

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // SSE frames are separated by blank lines
      let idx: number
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        const line = frame.split('\n').find(l => l.startsWith('data: '))
        if (!line) continue
        try {
          const payload = JSON.parse(line.slice(6))
          if (payload.type === 'stage') {
            callbacks.onStage(payload.stage, payload.message)
          } else if (payload.type === 'token') {
            callbacks.onToken(payload.content)
          } else if (payload.type === 'result') {
            const { type, ...rest } = payload
            void type
            callbacks.onResult(rest as UploadResult)
          } else if (payload.type === 'assistant_message') {
            callbacks.onAssistantMessage?.(payload.message as ChatMessage)
          } else if (payload.type === 'knowledge_preview') {
            const { type, ...rest } = payload
            void type
            callbacks.onKnowledgePreview?.(rest as UploadKnowledgePreview)
          } else if (payload.type === 'staged') {
            const { type, ...rest } = payload
            void type
            callbacks.onStaged?.(rest as StagedPayload)
          } else if (payload.type === 'module_auto_classified') {
            const { type, ...rest } = payload
            void type
            callbacks.onModuleAutoClassified?.(rest as ModuleAutoClassifiedPayload)
          } else if (payload.type === 'error') {
            sawTerminal = true
            callbacks.onError(payload.message)
          } else if (payload.type === 'done') {
            sawTerminal = true
            callbacks.onDone()
          }
        } catch {
          // ignore parse errors on partial frames
        }
      }
    }
    // 循环因 EOF 正常结束，但从未收到 done/error 帧 → 连接被中途掐断（如后端重启）。
    // 走 onError 让上层给出可见提示并复位，避免"既不 done 也不 error"的僵状态。
    if (!sawTerminal) callbacks.onError('连接中断（未收到结束帧），请重试')
  }).catch(err => {
    // abort（用户主动停止）走 onDone 让上层把 streaming/generating 等状态清掉，
    // 不要走 onError 弹"异常"红字——用户自己点的就不算错。
    if (err.name === 'AbortError') callbacks.onDone()
    else callbacks.onError(String(err))
  })

  return () => controller.abort()
}

// ── Lark (Feishu) URL import ──────────────────────────────────────────────────
// 通过本机已登录的 lark-cli 抓取飞书文档原文 → 走与 streamUpload 完全一致的事件序列。
// 回调签名复用 UploadStreamCallbacks，UI 不用区分来源。

export function streamLarkImport(
  url: string,
  sessionId: number,
  callbacks: UploadStreamCallbacks,
  moduleId?: number,
  role: 'prd' | 'mindmap' = 'prd',
): () => void {
  const controller = new AbortController()

  fetch(`${BASE_URL}/api/upload/lark/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ url, session_id: sessionId, module_id: moduleId, role }),
    signal: controller.signal,
  }).then(async res => {
    if (!res.ok || !res.body) {
      callbacks.onError(`HTTP ${res.status}`)
      return
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let sawTerminal = false  // 是否收到过 done/error 终止帧；用于识别"连接被中途掐断"

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let idx: number
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        const line = frame.split('\n').find(l => l.startsWith('data: '))
        if (!line) continue
        try {
          const payload = JSON.parse(line.slice(6))
          if (payload.type === 'stage') {
            callbacks.onStage(payload.stage, payload.message)
          } else if (payload.type === 'token') {
            callbacks.onToken(payload.content)
          } else if (payload.type === 'result') {
            const { type, ...rest } = payload
            void type
            callbacks.onResult(rest as UploadResult)
          } else if (payload.type === 'assistant_message') {
            callbacks.onAssistantMessage?.(payload.message as ChatMessage)
          } else if (payload.type === 'knowledge_preview') {
            const { type, ...rest } = payload
            void type
            callbacks.onKnowledgePreview?.(rest as UploadKnowledgePreview)
          } else if (payload.type === 'staged') {
            const { type, ...rest } = payload
            void type
            callbacks.onStaged?.(rest as StagedPayload)
          } else if (payload.type === 'module_auto_classified') {
            const { type, ...rest } = payload
            void type
            callbacks.onModuleAutoClassified?.(rest as ModuleAutoClassifiedPayload)
          } else if (payload.type === 'error') {
            sawTerminal = true
            callbacks.onError(payload.message)
          } else if (payload.type === 'done') {
            sawTerminal = true
            callbacks.onDone()
          }
        } catch {
          // ignore parse errors on partial frames
        }
      }
    }
    // 循环因 EOF 正常结束，但从未收到 done/error 帧 → 连接被中途掐断（如后端重启）。
    // 走 onError 让上层给出可见提示并复位，避免"既不 done 也不 error"的僵状态。
    if (!sawTerminal) callbacks.onError('连接中断（未收到结束帧），请重试')
  }).catch(err => {
    // abort（用户主动停止）走 onDone 让上层把 streaming/generating 等状态清掉，
    // 不要走 onError 弹"异常"红字——用户自己点的就不算错。
    if (err.name === 'AbortError') callbacks.onDone()
    else callbacks.onError(String(err))
  })

  return () => controller.abort()
}

// ── Mindmap (.md) upload ──────────────────────────────────────────────────────
// 与 streamUpload 几乎同构，只是端点改为 /api/upload/mindmap/stream，
// 后端会写入 Document(role="mindmap") + ClarificationState.mindmap_document_id。

export function streamMindmapUpload(
  file: File,
  sessionId: number,
  callbacks: UploadStreamCallbacks,
  moduleId?: number,
): () => void {
  const form = new FormData()
  form.append('file', file)
  form.append('session_id', String(sessionId))
  if (moduleId !== undefined) form.append('module_id', String(moduleId))

  const controller = new AbortController()

  fetch(`${BASE_URL}/api/upload/mindmap/stream`, {
    method: 'POST',
    body: form,
    headers: authHeaders(),
    signal: controller.signal,
  }).then(async res => {
    if (!res.ok || !res.body) {
      callbacks.onError(`HTTP ${res.status}`)
      return
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let sawTerminal = false  // 是否收到过 done/error 终止帧；用于识别"连接被中途掐断"

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let idx: number
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        const line = frame.split('\n').find(l => l.startsWith('data: '))
        if (!line) continue
        try {
          const payload = JSON.parse(line.slice(6))
          if (payload.type === 'stage') {
            callbacks.onStage(payload.stage, payload.message)
          } else if (payload.type === 'token') {
            callbacks.onToken(payload.content)
          } else if (payload.type === 'result') {
            const { type, ...rest } = payload
            void type
            callbacks.onResult(rest as UploadResult)
          } else if (payload.type === 'assistant_message') {
            callbacks.onAssistantMessage?.(payload.message as ChatMessage)
          } else if (payload.type === 'knowledge_preview') {
            const { type, ...rest } = payload
            void type
            callbacks.onKnowledgePreview?.(rest as UploadKnowledgePreview)
          } else if (payload.type === 'staged') {
            const { type, ...rest } = payload
            void type
            callbacks.onStaged?.(rest as StagedPayload)
          } else if (payload.type === 'module_auto_classified') {
            const { type, ...rest } = payload
            void type
            callbacks.onModuleAutoClassified?.(rest as ModuleAutoClassifiedPayload)
          } else if (payload.type === 'error') {
            sawTerminal = true
            callbacks.onError(payload.message)
          } else if (payload.type === 'done') {
            sawTerminal = true
            callbacks.onDone()
          }
        } catch {
          // ignore parse errors on partial frames
        }
      }
    }
    // 循环因 EOF 正常结束，但从未收到 done/error 帧 → 连接被中途掐断（如后端重启）。
    // 走 onError 让上层给出可见提示并复位，避免"既不 done 也不 error"的僵状态。
    if (!sawTerminal) callbacks.onError('连接中断（未收到结束帧），请重试')
  }).catch(err => {
    // abort（用户主动停止）走 onDone 让上层把 streaming/generating 等状态清掉，
    // 不要走 onError 弹"异常"红字——用户自己点的就不算错。
    if (err.name === 'AbortError') callbacks.onDone()
    else callbacks.onError(String(err))
  })

  return () => controller.abort()
}

// 飞书测试脑图导入：复用 streamLarkImport 但把 role 固定为 mindmap，
// 后端会用 mindmap_parser 重新解析 lark 抓回的 markdown 文本，并写入
// Document(role='mindmap') + ClarificationState.mindmap_document_id。
export function streamLarkMindmapImport(
  url: string,
  sessionId: number,
  callbacks: UploadStreamCallbacks,
  moduleId?: number,
): () => void {
  return streamLarkImport(url, sessionId, callbacks, moduleId, 'mindmap')
}

// ── Pipeline start（「开始生成」闸门） ─────────────────────────────────────────
// 上传只暂存文档。用户备齐资料后点「开始生成」调它，后端才跑模块自动分类 + 知识检索预览，
// 发 module_auto_classified / knowledge_preview 帧，并把状态推进到 awaiting_clarification。
// 复用 UploadStreamCallbacks（onModuleAutoClassified / onKnowledgePreview / onError / onDone）。
export function streamPipelineStart(
  sessionId: number,
  callbacks: UploadStreamCallbacks,
  moduleId?: number,
): () => void {
  const controller = new AbortController()

  fetch(`${BASE_URL}/api/pipeline/start/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ session_id: sessionId, module_id: moduleId }),
    signal: controller.signal,
  }).then(async res => {
    if (!res.ok || !res.body) {
      callbacks.onError(`HTTP ${res.status}`)
      return
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let sawTerminal = false

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let idx: number
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        const line = frame.split('\n').find(l => l.startsWith('data: '))
        if (!line) continue
        try {
          const payload = JSON.parse(line.slice(6))
          if (payload.type === 'stage') {
            callbacks.onStage(payload.stage, payload.message)
          } else if (payload.type === 'token') {
            callbacks.onToken(payload.content)
          } else if (payload.type === 'assistant_message') {
            callbacks.onAssistantMessage?.(payload.message as ChatMessage)
          } else if (payload.type === 'knowledge_preview') {
            const { type, ...rest } = payload
            void type
            callbacks.onKnowledgePreview?.(rest as UploadKnowledgePreview)
          } else if (payload.type === 'module_auto_classified') {
            const { type, ...rest } = payload
            void type
            callbacks.onModuleAutoClassified?.(rest as ModuleAutoClassifiedPayload)
          } else if (payload.type === 'error') {
            sawTerminal = true
            callbacks.onError(payload.message)
          } else if (payload.type === 'done') {
            sawTerminal = true
            callbacks.onDone()
          }
        } catch {
          // ignore parse errors on partial frames
        }
      }
    }
    if (!sawTerminal) callbacks.onError('连接中断（未收到结束帧），请重试')
  }).catch(err => {
    if (err.name === 'AbortError') callbacks.onDone()
    else callbacks.onError(String(err))
  })

  return () => controller.abort()
}

// ── Initial clarification (B 方案：用户勾完知识库后才跑 Clarifier) ─────────────
// upload/lark stream 跑到"persisted"就停，发一帧 knowledge_preview。前端确认后调这个端点
// 才真正跑 Clarifier，事件序列与原 upload stream 末段一致：token* + assistant_message + result + done。

export function streamInitialClarification(
  body: {
    sessionId: number
    documentId?: number | null
    mindmapDocumentId?: number | null
    knowledgeIds: number[] | null
  },
  callbacks: UploadStreamCallbacks,
): () => void {
  const controller = new AbortController()

  fetch(`${BASE_URL}/api/clarify/initial/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      session_id: body.sessionId,
      document_id: body.documentId ?? null,
      mindmap_document_id: body.mindmapDocumentId ?? null,
      knowledge_ids: body.knowledgeIds,
    }),
    signal: controller.signal,
  }).then(async res => {
    if (!res.ok || !res.body) {
      callbacks.onError(`HTTP ${res.status}`)
      return
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        const line = frame.split('\n').find(l => l.startsWith('data: '))
        if (!line) continue
        try {
          const payload = JSON.parse(line.slice(6))
          if (payload.type === 'stage') {
            callbacks.onStage(payload.stage, payload.message)
          } else if (payload.type === 'token') {
            callbacks.onToken(payload.content)
          } else if (payload.type === 'result') {
            const { type, ...rest } = payload
            void type
            callbacks.onResult(rest as UploadResult)
          } else if (payload.type === 'assistant_message') {
            callbacks.onAssistantMessage?.(payload.message as ChatMessage)
          } else if (payload.type === 'error') {
            callbacks.onError(payload.message)
          } else if (payload.type === 'done') {
            callbacks.onDone()
          }
        } catch {
          // ignore partial frames
        }
      }
    }
  }).catch(err => {
    // abort（用户主动停止）走 onDone 让上层把 streaming/generating 等状态清掉，
    // 不要走 onError 弹"异常"红字——用户自己点的就不算错。
    if (err.name === 'AbortError') callbacks.onDone()
    else callbacks.onError(String(err))
  })

  return () => controller.abort()
}

// ── Follow-up clarification ───────────────────────────────────────────────────

export interface ClarificationRoundHistory {
  questions: ClarificationQuestion[]
  answers: Record<string, string>
}

export interface FollowupResult {
  round: number
  max_rounds: number
  clarification: {
    summary: string
    module_detected: string
    case_prefix_suggestion?: string
    questions: ClarificationQuestion[]
    ready_to_generate: boolean
  }
}

export interface FollowupCallbacks {
  onStage: (stage: string, message: string, round?: number, maxRounds?: number) => void
  onToken: (text: string) => void
  onResult: (result: FollowupResult) => void
  onAssistantMessage?: (msg: ChatMessage) => void
  onError: (message: string) => void
  onDone: () => void
}

export function streamFollowupClarification(
  body: {
    sessionId: number
    documentId?: number | null
    mindmapDocumentId?: number | null
    moduleName: string
    casePrefix: string
    rounds: ClarificationRoundHistory[]
  },
  callbacks: FollowupCallbacks,
): () => void {
  const controller = new AbortController()

  fetch(`${BASE_URL}/api/clarify/followup/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      session_id: body.sessionId,
      document_id: body.documentId ?? null,
      mindmap_document_id: body.mindmapDocumentId ?? null,
      module_name: body.moduleName,
      case_prefix: body.casePrefix,
      rounds: body.rounds,
    }),
    signal: controller.signal,
  }).then(async res => {
    if (!res.ok || !res.body) {
      callbacks.onError(`HTTP ${res.status}`)
      return
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        const line = frame.split('\n').find(l => l.startsWith('data: '))
        if (!line) continue
        try {
          const payload = JSON.parse(line.slice(6))
          if (payload.type === 'stage') {
            callbacks.onStage(payload.stage, payload.message, payload.round, payload.max_rounds)
          } else if (payload.type === 'token') {
            callbacks.onToken(payload.content)
          } else if (payload.type === 'result') {
            const { type, ...rest } = payload
            void type
            callbacks.onResult(rest as FollowupResult)
          } else if (payload.type === 'assistant_message') {
            callbacks.onAssistantMessage?.(payload.message as ChatMessage)
          } else if (payload.type === 'error') {
            callbacks.onError(payload.message)
          } else if (payload.type === 'done') {
            callbacks.onDone()
          }
        } catch {
          // ignore partial frames
        }
      }
    }
  }).catch(err => {
    // abort（用户主动停止）走 onDone 让上层把 streaming/generating 等状态清掉，
    // 不要走 onError 弹"异常"红字——用户自己点的就不算错。
    if (err.name === 'AbortError') callbacks.onDone()
    else callbacks.onError(String(err))
  })

  return () => controller.abort()
}

// ── Generate ──────────────────────────────────────────────────────────────────

// 知识库预览：生成前给前端展示会被注入的条目，让用户取消勾选不想要的。
// 后端 GET /api/knowledge/preview?session_id=X，session 没有 ClarificationState/document → hits 为空。
export interface KnowledgeHit {
  id: number
  module_id: number | null
  document_id: number | null
  knowledge_type: string
  content: string
  source: string
  confidence: number
  version: number
  created_at: string | null
  distance: number | null  // 余弦距离，越小越相关；fallback 路径为 null
}

export interface KnowledgePreview {
  document_id: number | null
  module_id: number | null
  hits: KnowledgeHit[]
}

export const fetchKnowledgePreview = (sessionId: number, topK = 8, signal?: AbortSignal) =>
  api.get<KnowledgePreview>('/api/knowledge/preview', {
    params: { session_id: sessionId, top_k: topK },
    signal,
  }).then(r => r.data)

// ── Pending knowledge drafts (人工审核入库闸门) ────────────────────────────────
// 上传完成后 LLM 抽取出的"产品规则 / 约束 / 术语…"草稿先暂存在 Document.pending_knowledge，
// 用户在前端审核面板勾选要保留的条目，调 confirm 才真正入库；点丢弃则全部抛弃。

export interface PendingKnowledgeResponse {
  document_id: number
  drafts: KnowledgeDraft[]
  settled: boolean   // true 表示后端 pending_knowledge 已为空（用户审完或本就没抽到）
}

export const fetchPendingKnowledge = (documentId: number) =>
  api.get<PendingKnowledgeResponse>(`/api/documents/${documentId}/pending_knowledge`).then(r => r.data)

// 三种入参形态：
//   - acceptedDrafts：用户已编辑的完整草稿（推荐，支持改 content / type）
//   - acceptedIndices=null：全部按原 pending 入库
//   - acceptedIndices=[]：一条都不入（等价于丢弃）
// module 选项：applyModule=true 时把入库/文档归属改到 moduleId（null=不归入模块）；
//              不传或 false 时沿用文档当前模块（保持既有行为）。
export const confirmPendingKnowledge = (
  documentId: number,
  payload: { acceptedDrafts: KnowledgeDraft[] } | { acceptedIndices: number[] | null },
  moduleChoice?: { applyModule: boolean; moduleId: number | null },
) => {
  const body: Record<string, unknown> =
    'acceptedDrafts' in payload
      ? { accepted_drafts: payload.acceptedDrafts }
      : { accepted_indices: payload.acceptedIndices }
  if (moduleChoice?.applyModule) {
    body.apply_module = true
    body.module_id = moduleChoice.moduleId
  }
  return api.post<{ document_id: number; stored: number; settled: boolean; module_id: number | null }>(
    `/api/documents/${documentId}/confirm_pending_knowledge`,
    body,
  ).then(r => r.data)
}

export const discardPendingKnowledge = (documentId: number) =>
  confirmPendingKnowledge(documentId, { acceptedIndices: [] })

// ── Knowledge entries: 项目级 list / search / edit / delete ────────────────────
// 后端 GET /api/knowledge 支持 q（语义检索）+ module_id（过滤）+ top_k（限制）。
// 不传 q → 按 confidence DESC + 时间倒序返回。

export interface KnowledgeStats {
  total: number
  recent_added: number
  recent_days: number
  by_type: { knowledge_type: string; count: number }[]
  by_module: { module_id: number | null; module_name: string | null; count: number }[]
  documents: { total: number; with_pending_drafts: number }
  module_coverage: { modules_total: number; modules_with_knowledge: number }
}

export const fetchKnowledgeStats = (recentDays = 7) =>
  api.get<KnowledgeStats>('/api/knowledge/stats', { params: { recent_days: recentDays } })
    .then(r => r.data)

// ── 反馈进化总览（进化闭环第二步）──────────────────────────────────────────
// 三出口（knowledge/skill/prompt）的 待消费/已消费 计数 + 归一 intent 分布。
export interface EvolutionSummary {
  outputs: Record<'knowledge' | 'skill' | 'prompt', { pending: number; consumed: number }>
  intent_distribution: Record<string, number>
  triaged_total: number
}

export const fetchEvolutionSummary = () =>
  api.get<EvolutionSummary>('/api/feedback/evolution/summary').then(r => r.data)

export const fetchProjectKnowledge = (params: {
  q?: string
  moduleId?: number | null   // null（注意：与 undefined 不同）= "项目级（无模块）"过滤
  topK?: number
} = {}) => {
  const query: Record<string, string | number | boolean> = {}
  if (params.q) query.q = params.q
  if (params.moduleId === null) {
    query.only_orphan = true
  } else if (params.moduleId !== undefined) {
    query.module_id = params.moduleId
  }
  if (params.topK != null) query.top_k = params.topK
  return api.get<KnowledgeHit[]>('/api/knowledge', { params: query }).then(r => r.data)
}

export const updateKnowledge = (
  entryId: number,
  patch: { content?: string; confidence?: number; applyModule?: boolean; moduleId?: number | null },
) => {
  const body: Record<string, unknown> = {}
  if (patch.content !== undefined) body.content = patch.content
  if (patch.confidence !== undefined) body.confidence = patch.confidence
  if (patch.applyModule) {
    body.apply_module = true
    body.module_id = patch.moduleId ?? null
  }
  return api.put<{ id: number; version: number; module_id: number | null }>(
    `/api/knowledge/${entryId}`, body,
  ).then(r => r.data)
}

export const deleteKnowledge = (entryId: number) =>
  api.delete<{ deleted: boolean; id: number }>(`/api/knowledge/${entryId}`).then(r => r.data)

// 从 PRD 生成测试脑图并写入飞书文档，返回可点击的飞书链接。
// sessionId 可选：从「知识库/模块详情」触发时不传（纯文档级操作，不往聊天流追加气泡）。
export const generateMindmap = (
  documentId: number,
  opts?: {
    sessionId?: number | null
    moduleId?: number | null
    moduleName?: string
    clarifications?: Record<string, string>
  },
) =>
  api.post<{
    url: string
    title: string
    document_id: number
    markdown: string
    assistant_message?: ChatMessage | null
  }>('/api/mindmap/generate', {
    document_id: documentId,
    session_id: opts?.sessionId ?? null,
    module_id: opts?.moduleId ?? null,
    module_name: opts?.moduleName,
    clarifications: opts?.clarifications ?? null,
  }).then(r => r.data)

export const generateCases = (
  sessionId: number,
  documentId: number | null | undefined,
  answers?: Record<string, string>,
  moduleName?: string,
  casePrefix?: string,
  knowledgeIds?: number[] | null,
  mindmapDocumentId?: number | null,
  signal?: AbortSignal,
) =>
  api.post<{
    total: number
    cases: TestCase[]
    assistant_message?: ChatMessage
    // 用户在 KnowledgePreviewPanel 上的"已确认 N 条"被后端作为一条系统气泡落库；
    // 这里把那条 Message 一起返回，前端 append 到聊天流即可（刷新后从 DB 也能读到）。
    knowledge_selection_message?: ChatMessage | null
  }>('/api/generate', {
    session_id: sessionId,
    document_id: documentId ?? null,
    mindmap_document_id: mindmapDocumentId ?? null,
    clarification_answers: answers,
    module_name: moduleName,
    case_prefix: casePrefix,
    // null → 后端自动 top-K；[] → 不注入；非空 → 仅这些
    knowledge_ids: knowledgeIds === undefined ? null : knowledgeIds,
  }, { signal }).then(r => r.data)

export const fetchSessionCases = (sessionId: number) =>
  api.get<{ total: number; cases: TestCase[] }>(`/api/sessions/${sessionId}/cases`).then(r => r.data)

export interface AllCasesItem extends TestCase {
  session_id: number
  session_title: string
  test_result: string
  created_at: string | null
}

export const fetchAllCases = () =>
  api.get<{ total: number; cases: AllCasesItem[] }>('/api/cases').then(r => r.data)

export function exportSessionUrl(sessionId: number): string {
  const params = new URLSearchParams()
  if (_token) params.set('token', _token)
  if (_projectId != null) params.set('project_id', String(_projectId))
  return `${BASE_URL}/api/export/${sessionId}?${params.toString()}`
}

// 把任意一组用例（按 id）导出成 Excel 并触发浏览器下载。
// 用 POST + responseType=blob，不依赖 query token，规避 URL 长度限制。
export async function exportFilteredCases(caseIds: number[]): Promise<void> {
  if (caseIds.length === 0) return
  const res = await api.post('/api/export/cases', { case_ids: caseIds }, {
    responseType: 'blob',
  })
  const blob = res.data as Blob
  const filename = `testcases_filtered_${caseIds.length}.xlsx`
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

// ── Feedback ──────────────────────────────────────────────────────────────────

export const submitFeedback = (
  testCaseId: number,
  feedbackType: 'like' | 'dislike' | 'edit',
  modified?: Record<string, string>,
  original?: Record<string, string>,
  reason?: string,
) =>
  api.post('/api/feedback', {
    test_case_id: testCaseId,
    feedback_type: feedbackType,
    original_content: original,
    modified_content: modified,
    reason: reason,
  }).then(r => r.data)

// ── Cases (delete) ────────────────────────────────────────────────────────────

export const deleteTestCase = (caseId: number) =>
  api.delete(`/api/cases/${caseId}`).then(r => r.data)

// ── Modules ───────────────────────────────────────────────────────────────────

export interface ModuleSummary {
  id: number
  name: string
  code: string | null       // 英文名 = 用例编号前缀
  description: string | null
  parent_id: number | null
}

export const fetchModules = () =>
  api.get<ModuleSummary[]>('/api/modules').then(r => r.data)

export const createModule = (data: { name: string; code?: string | null; description?: string | null; parent_id?: number | null }) =>
  api.post<{ id: number; name: string; code: string | null }>('/api/modules', data).then(r => r.data)

export const updateModule = (
  id: number,
  data: { name?: string; code?: string | null; description?: string | null; parent_id?: number | null },
) =>
  api.put<ModuleSummary>(`/api/modules/${id}`, data).then(r => r.data)

export const deleteModule = (id: number) =>
  api.delete<{ ok: boolean }>(`/api/modules/${id}`).then(r => r.data)

// ── Documents（需求文档 / 脑图，按模块聚合查看） ──────────────────────────────

export interface DocumentSummary {
  id: number
  filename: string
  file_type: string       // docx / pdf / lark_doc / lark_wiki / lark_docs / lark_sheet / mindmap_md
  source_type: string     // file / lark
  source_url: string | null
  role: string            // prd / mindmap
  module_id: number | null
  uploaded_at: string | null
}

export interface DocumentDetail extends DocumentSummary {
  content: string          // 截断后的解析正文（只读预览）
  raw_text_length: number  // 原始解析正文总字符数（截断前）
  truncated: boolean       // content 是否相对原文被截断
}

export const fetchDocuments = (opts?: { moduleId?: number | null; onlyOrphan?: boolean; role?: string }) => {
  const params: Record<string, unknown> = {}
  if (opts?.onlyOrphan) params.only_orphan = true
  else if (opts?.moduleId != null) params.module_id = opts.moduleId
  if (opts?.role) params.role = opts.role
  return api.get<DocumentSummary[]>('/api/documents', { params }).then(r => r.data)
}

export const fetchDocumentDetail = (id: number) =>
  api.get<DocumentDetail>(`/api/documents/${id}`).then(r => r.data)

export const updateDocumentModule = (id: number, moduleId: number | null) =>
  api.patch<DocumentSummary>(`/api/documents/${id}`, { module_id: moduleId }).then(r => r.data)

// ── Module relations ──────────────────────────────────────────────────────────
// 模块间关系：A —[depends_on / triggers / shares_data / blocks / extends]→ B
// Generator 在生成用例前会自动按当前模块拉取相关 relations 注入 prompt。

export type ModuleRelationType = 'depends_on' | 'triggers' | 'shares_data' | 'blocks' | 'extends'

export interface ModuleRelation {
  id: number
  source_module_id: number
  target_module_id: number
  source_module_name: string | null
  target_module_name: string | null
  relation_type: ModuleRelationType
  description: string | null
  created_at: string | null
}

export const fetchModuleRelations = (moduleId?: number | null) => {
  const params = moduleId != null ? { module_id: moduleId } : {}
  return api.get<ModuleRelation[]>('/api/module_relations', { params }).then(r => r.data)
}

export const createModuleRelation = (data: {
  source_module_id: number
  target_module_id: number
  relation_type: ModuleRelationType
  description?: string | null
}) =>
  api.post<ModuleRelation>('/api/module_relations', data).then(r => r.data)

export const deleteModuleRelation = (relationId: number) =>
  api.delete<{ deleted: boolean; id: number }>(`/api/module_relations/${relationId}`).then(r => r.data)

// ── Streaming chat ────────────────────────────────────────────────────────────

export function streamChat(
  message: string,
  sessionId: number | null,
  onText: (text: string) => void,
  onDone: (newSessionId: number) => void,
  onError: (err: string) => void,
): () => void {
  const controller = new AbortController()

  fetch(`${BASE_URL}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ message, session_id: sessionId }),
    signal: controller.signal,
  }).then(async res => {
    if (!res.ok || !res.body) {
      onError(`HTTP ${res.status}`)
      return
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let resolvedSessionId = sessionId ?? 0

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      const lines = decoder.decode(value).split('\n')
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        try {
          const payload = JSON.parse(line.slice(6))
          if (payload.type === 'session') {
            resolvedSessionId = payload.session_id
          } else if (payload.type === 'text') {
            onText(payload.content)
          } else if (payload.type === 'done') {
            onDone(resolvedSessionId)
          } else if (payload.type === 'error') {
            onError(payload.content)
          }
        } catch {
          // ignore parse errors on partial lines
        }
      }
    }
  }).catch(err => {
    // 用户主动 abort 时 streamChat 没有 onDone 回调可调；
    // 调用方把 abort 当成"正常结束"自行清理 streaming 标志即可，这里静默。
    if (err.name !== 'AbortError') onError(String(err))
  })

  return () => controller.abort()
}

// ── Phase 4: Skills CRUD + 自动归纳 ────────────────────────────────────────

export interface SkillSummary {
  id: number
  name: string
  module_id: number | null
  source: string                    // manual / auto_generated
  version: number
  content?: string                  // GET list 不带；GET detail 才带
  created_at?: string | null
  updated_at?: string | null
}

export const fetchSkills = () =>
  api.get<SkillSummary[]>('/api/skills').then(r => r.data)

export const fetchSkillDetail = (skillId: number) =>
  api.get<SkillSummary>(`/api/skills/${skillId}`).then(r => r.data)

export const createSkill = (payload: {
  name: string
  content: string
  module_id?: number | null
}) =>
  api.post<SkillSummary>('/api/skills', payload).then(r => r.data)

export const updateSkill = (
  skillId: number,
  patch: { name?: string; content?: string; module_id?: number | null },
) =>
  api.put<SkillSummary>(`/api/skills/${skillId}`, patch).then(r => r.data)

export const deleteSkill = (skillId: number) =>
  api.delete<{ id: number; deleted: boolean }>(`/api/skills/${skillId}`).then(r => r.data)

export interface SkillRegenerateResult {
  created: boolean
  reason?: string
  action?: 'created' | 'updated'
  skill?: SkillSummary
  feedback_count: number
  knowledge_count: number
}

export const regenerateSkill = (moduleId: number) =>
  api.post<SkillRegenerateResult>('/api/skills/regenerate', { module_id: moduleId })
    .then(r => r.data)

// ── Phase 4: 最近修改沉淀（已分析的 edit 反馈列表） ─────────────────────────

export interface RecentFeedbackItem {
  id: number
  test_case_id: number
  test_case_name: string
  module: string | null
  intent: string | null
  summary: string | null
  changed_fields: string[]
  extracted_rule_count: number
  created_at: string | null
}

export const fetchRecentFeedback = (params: {
  moduleId?: number | null
  limit?: number
} = {}) => {
  const query: Record<string, string | number> = {}
  if (params.moduleId != null) query.module_id = params.moduleId
  if (params.limit != null) query.limit = params.limit
  return api.get<{ items: RecentFeedbackItem[] }>('/api/feedback/recent', { params: query })
    .then(r => r.data.items)
}

// ── 负反馈单独视图（每次归纳的相关记录）─────────────────────────────────────────
// edit 修改 + 带原因 dislike 的已分析反馈，含本次归纳出的规则全文与消费出口。
export interface NegativeFeedbackRule {
  knowledge_type: string | null
  content: string | null
  confidence: number | null
}

export interface NegativeFeedbackRecord {
  id: number
  feedback_type: 'edit' | 'dislike'
  test_case_id: number
  test_case_name: string
  module: string | null
  intent: string | null
  summary: string | null
  changed_fields: string[]
  reason: string | null
  triage_targets: string[]          // 分诊出口：knowledge / skill / prompt
  extracted_rules: NegativeFeedbackRule[]
  consumed_by: string[]             // 已被哪些出口消费
  created_at: string | null
}

export const fetchNegativeFeedback = (params: {
  moduleId?: number | null
  feedbackType?: 'edit' | 'dislike'
  limit?: number
} = {}) => {
  const query: Record<string, string | number> = {}
  if (params.moduleId != null) query.module_id = params.moduleId
  if (params.feedbackType) query.feedback_type = params.feedbackType
  if (params.limit != null) query.limit = params.limit
  return api.get<{ items: NegativeFeedbackRecord[] }>('/api/feedback/negative', { params: query })
    .then(r => r.data.items)
}

// ── Phase 4.2: System Prompt 版本化管理 ────────────────────────────────────

export interface PromptSummary {
  key: string
  purpose: string
  label: string
  description: string
  version_count: number
  using_default: boolean          // true = 当前生效的是代码默认（原始建议版本）
  active_version_id: number | null
  active_version: string | null
}

export interface PromptVersionItem {
  id: number
  prompt_id: string
  version: string
  template: string
  is_active: boolean
  created_by: number | null
  created_at: string | null
}

export interface PromptDefault {
  key: string
  label: string
  description: string
  template: string
}

export const fetchPrompts = () =>
  api.get<PromptSummary[]>('/api/prompts').then(r => r.data)

export const fetchPromptDefault = (key: string) =>
  api.get<PromptDefault>(`/api/prompts/${key}/default`).then(r => r.data)

export const fetchPromptVersions = (key: string) =>
  api.get<PromptVersionItem[]>(`/api/prompts/${key}/versions`).then(r => r.data)

export const createPromptVersion = (
  key: string,
  payload: { template: string; activate?: boolean; from_suggestion_id?: number },
) =>
  api.post<PromptVersionItem>(`/api/prompts/${key}/versions`, payload).then(r => r.data)

export const activatePromptVersion = (key: string, versionId: number) =>
  api.post<PromptVersionItem>(`/api/prompts/${key}/versions/${versionId}/activate`, {})
    .then(r => r.data)

export const resetPromptToDefault = (key: string) =>
  api.post<{ key: string; using_default: boolean }>(`/api/prompts/${key}/reset`, {})
    .then(r => r.data)

// ── Phase 4.2 二阶段: 系统给的 prompt 改进建议（只读建议 + 人工审核，本期仅 generator）──

export interface PromptSuggestion {
  id: number
  prompt_id: string
  base_version_id: number | null
  base_template: string
  suggested_template: string
  rationale: string | null
  evidence: {
    feedback_count?: number
    samples?: { intent?: string | null; summary?: string | null; changed_fields?: string[] | null }[]
  } | null
  status: string            // pending / adopted / dismissed
  created_at: string | null
}

export interface GenerateSuggestionResult {
  created: boolean
  reason?: string
  suggestion?: PromptSuggestion
  feedback_count: number
}

export const generatePromptSuggestion = (key: string) =>
  api.post<GenerateSuggestionResult>(`/api/prompts/${key}/suggestions/generate`, {})
    .then(r => r.data)

export const fetchPromptSuggestions = (key: string, status = 'pending') =>
  api.get<PromptSuggestion[]>(`/api/prompts/${key}/suggestions`, { params: { status_filter: status } })
    .then(r => r.data)

export const dismissPromptSuggestion = (suggestionId: number) =>
  api.post<{ id: number; status: string }>(`/api/prompts/suggestions/${suggestionId}/dismiss`, {})
    .then(r => r.data)
