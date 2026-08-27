import { MessageSquare, ListChecks, BookOpen, LineChart, Gauge } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { getCurrentUser } from '../api/client'

export type ViewKey = 'chat' | 'cases' | 'knowledge' | 'feedback' | 'usage'

interface Props {
  value: ViewKey
  onChange: (v: ViewKey) => void
}

const tabs: { key: ViewKey; label: string; icon: LucideIcon; adminOnly?: boolean }[] = [
  { key: 'chat', label: '对话', icon: MessageSquare },
  { key: 'cases', label: '用例管理', icon: ListChecks },
  { key: 'knowledge', label: '知识库', icon: BookOpen },
  { key: 'feedback', label: '进化报告', icon: LineChart },
  { key: 'usage', label: 'Token 用量', icon: Gauge, adminOnly: true },
]

export default function TabBar({ value, onChange }: Props) {
  return (
    <nav
      aria-label="主导航"
      className="w-14 flex-shrink-0 bg-gray-900 border-r border-gray-800 flex flex-col items-center py-3 gap-1"
    >
      {tabs.filter(t => !t.adminOnly || getCurrentUser()?.is_admin).map(t => {
        const Icon = t.icon
        const active = value === t.key
        return (
          <button
            key={t.key}
            type="button"
            onClick={() => onChange(t.key)}
            title={t.label}
            aria-label={t.label}
            aria-current={active ? 'page' : undefined}
            className={`group relative w-10 h-10 flex items-center justify-center rounded-lg transition-colors ${
              active
                ? 'bg-gray-800 text-white'
                : 'text-gray-400 hover:bg-gray-800/60 hover:text-gray-100'
            }`}
          >
            {/* Active indicator bar on the left edge */}
            <span
              className={`absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r ${
                active ? 'bg-amber-400' : 'bg-transparent'
              }`}
            />
            <Icon size={18} />
            {/* Hover tooltip */}
            <span className="pointer-events-none absolute left-12 top-1/2 -translate-y-1/2 whitespace-nowrap bg-gray-800 text-gray-100 text-xs px-2 py-1 rounded shadow opacity-0 group-hover:opacity-100 transition-opacity z-50">
              {t.label}
            </span>
          </button>
        )
      })}
    </nav>
  )
}
