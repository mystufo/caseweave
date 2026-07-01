import { useState, type KeyboardEvent } from 'react'
import { Plus, MessageSquare, Pencil, Check, X, Loader2 } from 'lucide-react'
import type { ChatSession } from '../api/client'

interface Props {
  sessions: ChatSession[]
  activeId: number | null
  busyIds?: Set<number>
  onSelect: (id: number) => void
  onNew: () => void
  onRename: (id: number, title: string) => Promise<void> | void
}

export default function SessionList({ sessions, activeId, busyIds, onSelect, onNew, onRename }: Props) {
  const [editingId, setEditingId] = useState<number | null>(null)
  const [draft, setDraft] = useState('')

  const startEdit = (s: ChatSession) => {
    setEditingId(s.id)
    setDraft(s.title)
  }

  const commit = async () => {
    if (editingId == null) return
    const title = draft.trim()
    if (title) await onRename(editingId, title)
    setEditingId(null)
  }

  const cancel = () => setEditingId(null)

  const handleKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      commit()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      cancel()
    }
  }

  return (
    <div className="flex flex-col h-full">
      <div className="p-4 border-b border-gray-200">
        <h1 className="text-lg font-bold text-gray-800 mb-3">TestCraft AI</h1>
        <button
          onClick={onNew}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
        >
          <Plus size={16} />
          新会话
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-2 space-y-1">
        {sessions.length === 0 && (
          <p className="text-xs text-gray-400 text-center py-4">暂无会话</p>
        )}
        {sessions.map(s => {
          const isActive = s.id === activeId
          const isEditing = editingId === s.id
          return (
            <div
              key={s.id}
              className={`group relative flex items-center gap-2 rounded-lg px-3 py-2.5 text-sm transition-colors ${
                isActive
                  ? 'bg-blue-50 text-blue-700 font-medium'
                  : 'text-gray-700 hover:bg-gray-100'
              }`}
            >
              <MessageSquare size={14} className="flex-shrink-0 opacity-60" />
              {busyIds?.has(s.id) && !isEditing && (
                <Loader2 size={12} className="flex-shrink-0 animate-spin text-amber-500" />
              )}
              {isEditing ? (
                <>
                  <input
                    autoFocus
                    value={draft}
                    onChange={e => setDraft(e.target.value)}
                    onKeyDown={handleKey}
                    onBlur={commit}
                    className="flex-1 bg-white border border-blue-300 rounded px-1.5 py-0.5 text-sm outline-none focus:border-blue-500"
                  />
                  <button
                    onMouseDown={e => { e.preventDefault(); commit() }}
                    className="text-green-600 hover:text-green-700"
                    title="保存"
                  >
                    <Check size={14} />
                  </button>
                  <button
                    onMouseDown={e => { e.preventDefault(); cancel() }}
                    className="text-gray-400 hover:text-red-500"
                    title="取消"
                  >
                    <X size={14} />
                  </button>
                </>
              ) : (
                <>
                  <button
                    onClick={() => onSelect(s.id)}
                    onDoubleClick={() => startEdit(s)}
                    className="flex-1 text-left truncate"
                    title="双击重命名"
                  >
                    {s.title}
                  </button>
                  <button
                    onClick={(e) => { e.stopPropagation(); startEdit(s) }}
                    className="opacity-0 group-hover:opacity-100 text-gray-400 hover:text-blue-600 transition-opacity flex-shrink-0"
                    title="重命名"
                  >
                    <Pencil size={12} />
                  </button>
                </>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
