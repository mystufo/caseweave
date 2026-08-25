/** toast 的呈现层。状态与 toast() 入口在 lib/toast.ts，这里只订阅并渲染。 */
import { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from 'lucide-react'
import { dismissToast, subscribeToasts, type ToastItem, type ToastKind } from '../lib/toast'

const STYLES: Record<ToastKind, { box: string; icon: typeof Info; iconClass: string }> = {
  info: { box: 'border-gray-200 bg-white', icon: Info, iconClass: 'text-gray-400' },
  success: { box: 'border-emerald-200 bg-emerald-50', icon: CheckCircle2, iconClass: 'text-emerald-500' },
  warning: { box: 'border-amber-200 bg-amber-50', icon: AlertTriangle, iconClass: 'text-amber-500' },
  error: { box: 'border-red-200 bg-red-50', icon: XCircle, iconClass: 'text-red-500' },
}

export function ToastHost() {
  const [list, setList] = useState<ToastItem[]>([])

  useEffect(() => subscribeToasts(setList), [])

  if (list.length === 0) return null

  return (
    <div className="fixed top-3 left-1/2 -translate-x-1/2 z-50 flex flex-col items-center gap-2 pointer-events-none">
      {list.map(t => {
        const s = STYLES[t.kind]
        const Icon = s.icon
        return (
          <div
            key={t.id}
            role="status"
            className={`pointer-events-auto flex items-start gap-2 max-w-lg px-3.5 py-2.5 rounded-lg border shadow-lg text-sm text-gray-800 ${s.box}`}
          >
            <Icon size={16} className={`flex-shrink-0 mt-0.5 ${s.iconClass}`} />
            <span className="leading-relaxed whitespace-pre-wrap">{t.message}</span>
            {t.action && (
              <button
                type="button"
                onClick={() => { dismissToast(t.id); t.action!.onClick() }}
                className="flex-shrink-0 ml-1 px-2 py-0.5 rounded-md text-xs font-medium text-white bg-gray-800 hover:bg-gray-700"
              >
                {t.action.label}
              </button>
            )}
            <button
              type="button"
              onClick={() => dismissToast(t.id)}
              className="flex-shrink-0 text-gray-400 hover:text-gray-600"
              aria-label="关闭"
            >
              <X size={14} />
            </button>
          </div>
        )
      })}
    </div>
  )
}
