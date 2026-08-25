/**
 * 模态提示框的状态存储（不含 UI，UI 见 components/AlertDialog.tsx）。
 *
 * 与 lib/toast.ts 的分工：toast 是"顺带说一声"，自己会消失；这里是"必须让你知道"，
 * 只有点确认才关得掉。并发闸门 / 每日配额把请求挡在门外就属于后者——用户下一步该干嘛
 * （等着 / 明天再来）取决于这条信息，飘一下就没了等于没说。
 *
 * 同样用模块级订阅表而非 Context：触发点埋在 ChatPage 层层嵌套的 SSE 回调闭包里。
 */
export interface AlertDialogAction {
  label: string
  onClick: () => void
}

export interface AlertDialogSpec {
  title: string
  message: string
  /** 确认按钮文案，默认「知道了」。点它才关闭。 */
  confirmLabel?: string
  /** 可选的次要按钮（如「重试」）。点击后先关闭再执行。 */
  action?: AlertDialogAction
  /**
   * 同 key 的框同时只允许存在一个：闸门 429 可能被多条链路同时触发
   * （比如一次导入里 PRD 和脑图两段），叠一堆一样的框只会烦人。
   */
  dedupeKey?: string
}

export interface AlertDialogItem extends AlertDialogSpec {
  id: number
}

type Listener = (item: AlertDialogItem | null) => void

let current: AlertDialogItem | null = null
let seq = 0
const listeners = new Set<Listener>()

function emit() {
  listeners.forEach(fn => fn(current))
}

export function subscribeAlertDialog(fn: Listener): () => void {
  listeners.add(fn)
  fn(current)
  return () => { listeners.delete(fn) }
}

export function closeAlertDialog(id?: number) {
  if (id != null && current?.id !== id) return
  current = null
  emit()
}

export function showAlertDialog(spec: AlertDialogSpec): number | null {
  // 已经开着一个同 key 的框 → 忽略这次，别把用户按了一次确认又冒出一个一模一样的。
  if (spec.dedupeKey && current?.dedupeKey === spec.dedupeKey) return null
  current = { ...spec, id: ++seq }
  emit()
  return current.id
}

/** 并发闸门 / 配额把请求挡在门外时的统一提示。retry 为空则只有确认按钮。 */
export function showGateBlocked(message: string, retry?: AlertDialogAction) {
  return showAlertDialog({
    title: '任务未开始',
    message,
    confirmLabel: '知道了',
    action: retry,
    dedupeKey: 'llm-gate',
  })
}
