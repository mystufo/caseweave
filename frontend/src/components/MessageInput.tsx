import { useRef, type KeyboardEvent } from 'react'
import { Send, Upload, Network, Link as LinkIcon, Pencil } from 'lucide-react'

interface Props {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  onFileSelect: (file: File) => void
  onMindmapSelect?: (file: File) => void
  onMindmapPaste?: () => void
  onLarkImport?: () => void
  disabled?: boolean
  placeholder?: string
}

export default function MessageInput({
  value, onChange, onSend, onFileSelect, onMindmapSelect, onMindmapPaste,
  onLarkImport, disabled, placeholder,
}: Props) {
  const fileRef = useRef<HTMLInputElement>(null)
  const mindmapRef = useRef<HTMLInputElement>(null)

  const handleKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (!disabled && value.trim()) onSend()
    }
  }

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) onFileSelect(file)
    e.target.value = ''
  }

  const handleMindmap = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) onMindmapSelect?.(file)
    e.target.value = ''
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => fileRef.current?.click()}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-700 bg-white border border-gray-200 rounded-full hover:border-blue-300 hover:text-blue-600 hover:bg-blue-50/50 transition-colors disabled:opacity-50"
          title="上传 PRD 文档（.docx / .pdf）"
          type="button"
          disabled={disabled}
        >
          <Upload size={15} />
          上传需求文档
        </button>

        {onMindmapSelect && (
          <button
            onClick={() => mindmapRef.current?.click()}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-700 bg-white border border-gray-200 rounded-full hover:border-emerald-300 hover:text-emerald-600 hover:bg-emerald-50/50 transition-colors disabled:opacity-50"
            title="上传测试脑图（Markdown 大纲 .md）"
            type="button"
            disabled={disabled}
          >
            <Network size={15} />
            上传测试脑图
          </button>
        )}

        {onMindmapPaste && (
          <button
            onClick={onMindmapPaste}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-700 bg-white border border-gray-200 rounded-full hover:border-emerald-300 hover:text-emerald-600 hover:bg-emerald-50/50 transition-colors disabled:opacity-50"
            title="粘贴测试脑图大纲（Markdown 文本）"
            type="button"
            disabled={disabled}
          >
            <Pencil size={15} />
            粘贴脑图大纲
          </button>
        )}

        {onLarkImport && (
          <button
            onClick={onLarkImport}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-700 bg-white border border-gray-200 rounded-full hover:border-amber-300 hover:text-amber-600 hover:bg-amber-50/50 transition-colors disabled:opacity-50"
            title="从飞书链接导入需求 / 测试脑图"
            type="button"
            disabled={disabled}
          >
            <LinkIcon size={15} />
            飞书链接
          </button>
        )}

        <input
          ref={fileRef}
          type="file"
          accept=".docx,.pdf"
          className="hidden"
          onChange={handleFile}
        />

        <input
          ref={mindmapRef}
          type="file"
          accept=".md,.markdown,text/markdown"
          className="hidden"
          onChange={handleMindmap}
        />
      </div>

      <div className="flex items-end gap-2 bg-white border border-gray-300 rounded-2xl p-2 shadow-sm focus-within:border-blue-400 focus-within:ring-1 focus-within:ring-blue-400 transition-all">
        <textarea
          className="flex-1 resize-none outline-none text-sm text-gray-800 placeholder-gray-400 max-h-40 min-h-[20px] leading-relaxed px-2 py-1.5"
          rows={1}
          value={value}
          onChange={e => onChange(e.target.value)}
          onKeyDown={handleKey}
          disabled={disabled}
          placeholder={placeholder ?? '输入消息…（Enter 发送，Shift+Enter 换行）'}
          style={{ height: 'auto' }}
          onInput={e => {
            const el = e.currentTarget
            el.style.height = 'auto'
            el.style.height = `${Math.min(el.scrollHeight, 160)}px`
          }}
        />

        <button
          onClick={onSend}
          disabled={disabled || !value.trim()}
          className="p-2 bg-blue-600 text-white rounded-xl hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex-shrink-0"
          type="button"
        >
          <Send size={18} />
        </button>
      </div>
    </div>
  )
}
