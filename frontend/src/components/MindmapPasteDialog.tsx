import { useEffect, useState } from 'react'
import { Pencil, X, Loader2, Network } from 'lucide-react'

interface Props {
  open: boolean
  onClose: () => void
  onSubmit: (text: string, filename: string) => void
  loading?: boolean
}

const SAMPLE = `# 登录
- 账号密码登录
  - 正常登录
  - 密码错误
  - 账号被锁定
- 短信验证码登录
  - 验证码正确
  - 验证码超时`

export default function MindmapPasteDialog({ open, onClose, onSubmit, loading }: Props) {
  const [text, setText] = useState('')
  const [filename, setFilename] = useState('粘贴的脑图')
  const [error, setError] = useState<string | null>(null)

  // 关闭时清空表单，同 LarkUrlDialog：只在 open 翻转时跑一次，组件随后返回 null。
  useEffect(() => {
    if (!open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setText('')
      setFilename('粘贴的脑图')
      setError(null)
    }
  }, [open])

  if (!open) return null

  const trimmed = text.trim()
  const lineCount = trimmed ? trimmed.split('\n').length : 0
  const charCount = trimmed.length

  const handleSubmit = () => {
    if (!trimmed) {
      setError('请粘贴 Markdown 大纲（# 标题 / - 列表项）')
      return
    }
    setError(null)
    const safeName = filename.trim() || '粘贴的脑图'
    onSubmit(trimmed, safeName)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-20 bg-black/30" onClick={onClose}>
      <div
        className="w-full max-w-2xl bg-white rounded-2xl shadow-xl border border-emerald-200 p-5 space-y-4 mx-4"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Network size={18} className="text-emerald-500" />
            <h3 className="text-base font-semibold text-gray-900">粘贴测试脑图大纲</h3>
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
          直接粘贴 Markdown 大纲（<code className="px-1 py-0.5 bg-gray-100 rounded text-[11px]">#</code> 标题、
          <code className="px-1 py-0.5 bg-gray-100 rounded text-[11px]">-</code> /
          <code className="px-1 py-0.5 bg-gray-100 rounded text-[11px]">*</code> 列表项），
          系统会按层级解析为测试脑图节点，行为与上传 <code className="px-1 py-0.5 bg-gray-100 rounded text-[11px]">.md</code> 文件一致。
        </p>

        <div className="space-y-1">
          <label className="text-xs font-medium text-gray-600">脑图名称</label>
          <input
            type="text"
            value={filename}
            onChange={e => setFilename(e.target.value)}
            disabled={loading}
            maxLength={60}
            className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg outline-none transition-colors focus:border-emerald-400"
          />
        </div>

        <div className="space-y-1">
          <div className="flex items-center justify-between">
            <label className="text-xs font-medium text-gray-600">大纲内容</label>
            <span className="text-[11px] text-gray-400">
              {lineCount > 0 ? `${lineCount} 行 / ${charCount} 字` : '示例如右侧 placeholder'}
            </span>
          </div>
          <textarea
            value={text}
            onChange={e => {
              setText(e.target.value)
              if (error) setError(null)
            }}
            placeholder={SAMPLE}
            disabled={loading}
            rows={12}
            autoFocus
            className={`w-full px-3 py-2 text-sm font-mono border rounded-lg outline-none transition-colors resize-y leading-relaxed ${
              error
                ? 'border-red-300 focus:border-red-400'
                : trimmed
                  ? 'border-emerald-300 focus:border-emerald-400'
                  : 'border-gray-300 focus:border-emerald-400'
            }`}
          />
          {error && <div className="text-xs text-red-600">{error}</div>}
        </div>

        <div className="flex items-center justify-end gap-2 pt-1">
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
            disabled={loading || !trimmed}
            type="button"
            className="flex items-center gap-1.5 px-4 py-1.5 text-sm text-white rounded-lg disabled:opacity-40 disabled:cursor-not-allowed transition-colors bg-emerald-500 hover:bg-emerald-600"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <Pencil size={14} />}
            {loading ? '导入中…' : '导入'}
          </button>
        </div>
      </div>
    </div>
  )
}
