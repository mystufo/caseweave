import { useEffect, useState } from 'react'
import { Link as LinkIcon, X, Loader2, FileText, Network } from 'lucide-react'

export interface LarkUrlSubmit {
  prdUrl: string | null
  mindmapUrl: string | null
}

interface Props {
  open: boolean
  onClose: () => void
  onSubmit: (urls: LarkUrlSubmit) => void
  loading?: boolean
}

// 与后端 lark_fetcher.classify_lark_url 同款正则（前端先验，避免一次后端往返）
const LARK_URL_RE = /^https?:\/\/[\w.-]*(?:feishu|larksuite|lark)\.[\w.-]+\/(?:docx|wiki|docs|sheets?|sheet)\/[A-Za-z0-9]{15,}/i

export default function LarkUrlDialog({ open, onClose, onSubmit, loading }: Props) {
  const [prdUrl, setPrdUrl] = useState('')
  const [mindmapUrl, setMindmapUrl] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) {
      setPrdUrl('')
      setMindmapUrl('')
      setError(null)
    }
  }, [open])

  if (!open) return null

  const prdTrimmed = prdUrl.trim()
  const mindmapTrimmed = mindmapUrl.trim()
  const prdValid = !prdTrimmed || LARK_URL_RE.test(prdTrimmed)
  const mindmapValid = !mindmapTrimmed || LARK_URL_RE.test(mindmapTrimmed)
  const hasAny = !!(prdTrimmed || mindmapTrimmed)
  const canSubmit = hasAny && prdValid && mindmapValid

  const handleSubmit = () => {
    if (!hasAny) {
      setError('至少填写一个飞书链接（PRD 或测试脑图）')
      return
    }
    if (!prdValid) {
      setError('PRD 链接格式不对（仅支持 docx / wiki / docs / sheets 路径）')
      return
    }
    if (!mindmapValid) {
      setError('脑图链接格式不对（仅支持 docx / wiki / docs / sheets 路径）')
      return
    }
    setError(null)
    onSubmit({
      prdUrl: prdTrimmed || null,
      mindmapUrl: mindmapTrimmed || null,
    })
  }

  const handleKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !loading && canSubmit) handleSubmit()
    if (e.key === 'Escape') onClose()
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-24 bg-black/30" onClick={onClose}>
      <div
        className="w-full max-w-xl bg-white rounded-2xl shadow-xl border border-amber-200 p-5 space-y-4 mx-4"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <LinkIcon size={18} className="text-amber-500" />
            <h3 className="text-base font-semibold text-gray-900">从飞书链接导入</h3>
          </div>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 transition-colors"
            type="button"
            aria-label="关闭"
          >
            <X size={18} />
          </button>
        </div>

        <p className="text-xs text-gray-500 leading-relaxed">
          后端会调用本机 <code className="px-1 py-0.5 bg-gray-100 rounded text-[11px]">lark-cli</code> 抓取文档原文。
          两个链接都可填、也可只填一个：仅 PRD 走需求文档流程，仅脑图按 Markdown 大纲解析为测试脑图，
          同时填则两份内容并排注入提示词，<strong>冲突时以脑图为准</strong>。
          支持 <strong>新版文档（docx）</strong>、<strong>知识库（wiki）</strong>、<strong>旧版文档（docs）</strong>；电子表格暂不支持。
        </p>

        <div className="space-y-3">
          <div className="space-y-1">
            <label className="flex items-center gap-1.5 text-xs font-medium text-amber-700">
              <FileText size={12} />
              PRD 文档链接（可选）
            </label>
            <input
              type="url"
              value={prdUrl}
              onChange={e => {
                setPrdUrl(e.target.value)
                if (error) setError(null)
              }}
              onKeyDown={handleKey}
              placeholder="https://xxx.feishu.cn/docx/xxxxxxxxxxxx"
              autoFocus
              disabled={loading}
              className={`w-full px-3 py-2 text-sm border rounded-lg outline-none transition-colors ${
                prdTrimmed && !prdValid
                  ? 'border-red-300 focus:border-red-400'
                  : prdTrimmed
                    ? 'border-amber-300 focus:border-amber-400'
                    : 'border-gray-300 focus:border-blue-400'
              }`}
            />
          </div>

          <div className="space-y-1">
            <label className="flex items-center gap-1.5 text-xs font-medium text-emerald-700">
              <Network size={12} />
              测试脑图链接（可选，Markdown 大纲）
            </label>
            <input
              type="url"
              value={mindmapUrl}
              onChange={e => {
                setMindmapUrl(e.target.value)
                if (error) setError(null)
              }}
              onKeyDown={handleKey}
              placeholder="https://xxx.feishu.cn/docx/xxxxxxxxxxxx"
              disabled={loading}
              className={`w-full px-3 py-2 text-sm border rounded-lg outline-none transition-colors ${
                mindmapTrimmed && !mindmapValid
                  ? 'border-red-300 focus:border-red-400'
                  : mindmapTrimmed
                    ? 'border-emerald-300 focus:border-emerald-400'
                    : 'border-gray-300 focus:border-blue-400'
              }`}
            />
          </div>

          {error && <div className="text-xs text-red-600">{error}</div>}
          {!error && hasAny && prdValid && mindmapValid && (
            <div className="text-xs text-gray-500">
              {prdTrimmed && mindmapTrimmed
                ? '两个链接均有效，将依次抓取 PRD 与脑图。'
                : prdTrimmed
                  ? '将仅抓取 PRD 文档。'
                  : '将仅抓取测试脑图。'}
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            disabled={loading}
            type="button"
            className="px-3 py-1.5 text-sm text-gray-600 border border-gray-200 rounded-lg hover:bg-gray-50 disabled:opacity-50"
          >
            取消
          </button>
          <button
            onClick={handleSubmit}
            disabled={loading || !canSubmit}
            type="button"
            className="flex items-center gap-1.5 px-4 py-1.5 text-sm text-white rounded-lg disabled:opacity-40 disabled:cursor-not-allowed transition-colors bg-amber-500 hover:bg-amber-600"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <LinkIcon size={14} />}
            {loading ? '导入中…' : '导入'}
          </button>
        </div>
      </div>
    </div>
  )
}
