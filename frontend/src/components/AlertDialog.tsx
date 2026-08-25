/** 模态提示框的呈现层。状态与入口在 lib/alertDialog.ts，这里只订阅并渲染。 */
import { useEffect, useRef, useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import { closeAlertDialog, subscribeAlertDialog, type AlertDialogItem } from '../lib/alertDialog'

export function AlertDialogHost() {
  const [item, setItem] = useState<AlertDialogItem | null>(null)
  const confirmRef = useRef<HTMLButtonElement>(null)

  useEffect(() => subscribeAlertDialog(setItem), [])

  // 打开时把焦点放到确认按钮上：键盘用户按回车就能关，不用去摸鼠标。
  // 刻意不监听 Esc、也不响应点击遮罩——这个框存在的意义就是"必须点确认才消失"。
  useEffect(() => {
    if (item) confirmRef.current?.focus()
  }, [item])

  if (!item) return null

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 px-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="alert-dialog-title"
    >
      <div className="w-full max-w-sm rounded-2xl bg-white shadow-2xl border border-gray-200 p-5">
        <div className="flex items-start gap-3">
          <span className="flex-shrink-0 w-9 h-9 rounded-full bg-amber-100 flex items-center justify-center">
            <AlertTriangle size={18} className="text-amber-600" />
          </span>
          <div className="min-w-0 flex-1">
            <h2 id="alert-dialog-title" className="text-sm font-semibold text-gray-900 mb-1">
              {item.title}
            </h2>
            <p className="text-sm text-gray-600 leading-relaxed whitespace-pre-wrap">
              {item.message}
            </p>
          </div>
        </div>
        <div className="mt-5 flex items-center justify-end gap-2">
          {item.action && (
            <button
              type="button"
              onClick={() => { const a = item.action!; closeAlertDialog(item.id); a.onClick() }}
              className="px-3.5 py-1.5 text-sm text-gray-700 border border-gray-300 rounded-full hover:bg-gray-50"
            >
              {item.action.label}
            </button>
          )}
          <button
            ref={confirmRef}
            type="button"
            onClick={() => closeAlertDialog(item.id)}
            className="px-4 py-1.5 text-sm text-white bg-blue-600 rounded-full hover:bg-blue-700"
          >
            {item.confirmLabel ?? '知道了'}
          </button>
        </div>
      </div>
    </div>
  )
}
