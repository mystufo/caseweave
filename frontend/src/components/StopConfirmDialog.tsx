import { AlertTriangle, X } from 'lucide-react'

interface Props {
  open: boolean
  taskLabel: string
  onClose: () => void
  onConfirm: () => void
}

export default function StopConfirmDialog({ open, taskLabel, onClose, onConfirm }: Props) {
  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-32 bg-black/30" onClick={onClose}>
      <div
        className="w-full max-w-md bg-white rounded-2xl shadow-xl border border-red-200 p-5 space-y-4 mx-4"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <AlertTriangle size={18} className="text-red-500" />
            <h3 className="text-base font-semibold text-gray-900">停止当前任务？</h3>
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

        <div className="text-sm text-gray-600 leading-relaxed">
          确认要停止「<span className="font-medium text-gray-800">{taskLabel}</span>」吗？
          <br />
          <span className="text-xs text-gray-500">已经持久化的内容（如已落库的文档/澄清记录）会保留；当前进行中的 LLM 调用会被中止，已消耗的 token 不会退还。</span>
        </div>

        <div className="flex items-center justify-end gap-2 pt-1">
          <button
            onClick={onClose}
            type="button"
            className="px-3 py-1.5 text-sm text-gray-600 border border-gray-200 rounded-lg hover:bg-gray-50"
          >
            取消
          </button>
          <button
            onClick={onConfirm}
            type="button"
            className="px-4 py-1.5 text-sm text-white rounded-lg transition-colors bg-red-500 hover:bg-red-600"
          >
            确定停止
          </button>
        </div>
      </div>
    </div>
  )
}
