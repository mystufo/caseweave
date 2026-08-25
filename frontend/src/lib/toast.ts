/**
 * 轻量 toast 的状态存储（不含 UI，UI 见 components/Toast.tsx）。
 *
 * 没引第三方库，也没走 React Context——提示的触发点大多埋在 ChatPage 里层层嵌套的
 * SSE 回调闭包（onError/onDone）中，透传 hook 要改一大片签名。模块级订阅表 +
 * 顶层挂一个 <ToastHost /> 是这里最省事、也最好测的做法。
 */
export type ToastKind = 'info' | 'success' | 'warning' | 'error'

export interface ToastItem {
  id: number
  kind: ToastKind
  message: string
  /** 可选操作按钮（如「重试」）。点击后 toast 自动关闭。 */
  action?: { label: string; onClick: () => void }
  /** 毫秒。带 action 的默认久一些，免得用户还没点就没了。0 = 不自动消失 */
  duration: number
}

export interface ShowToastOptions {
  action?: ToastItem['action']
  duration?: number
  /** 同 key 只保留最新一条，避免连点几次刷屏（并发闸门的提示尤其容易连发） */
  dedupeKey?: string
}

type Listener = (items: ToastItem[]) => void

let items: ToastItem[] = []
let seq = 0
const listeners = new Set<Listener>()
const dedupe = new Map<string, number>()

function emit() {
  const snapshot = [...items]
  listeners.forEach(fn => fn(snapshot))
}

export function subscribeToasts(fn: Listener): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

export function dismissToast(id: number) {
  items = items.filter(t => t.id !== id)
  emit()
}

export function showToast(kind: ToastKind, message: string, opts: ShowToastOptions = {}): number {
  if (opts.dedupeKey) {
    const prev = dedupe.get(opts.dedupeKey)
    if (prev != null) dismissToast(prev)
  }
  const id = ++seq
  const duration = opts.duration ?? (opts.action ? 10000 : 4500)
  items = [...items, { id, kind, message, action: opts.action, duration }]
  if (opts.dedupeKey) dedupe.set(opts.dedupeKey, id)
  emit()
  if (duration > 0) window.setTimeout(() => dismissToast(id), duration)
  return id
}

export const toast = {
  info: (m: string, o?: ShowToastOptions) => showToast('info', m, o),
  success: (m: string, o?: ShowToastOptions) => showToast('success', m, o),
  warning: (m: string, o?: ShowToastOptions) => showToast('warning', m, o),
  error: (m: string, o?: ShowToastOptions) => showToast('error', m, o),
}
