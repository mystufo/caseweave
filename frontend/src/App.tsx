import { useEffect, useState } from 'react'
import ChatPage from './pages/ChatPage'
import CasesPage from './pages/CasesPage'
import KnowledgePage from './pages/KnowledgePage'
import NegativeFeedbackPage from './pages/NegativeFeedbackPage'
import LoginPage from './pages/LoginPage'
import ProjectPickerPage from './pages/ProjectPickerPage'
import type { ViewKey } from './components/TabBar'
import {
  fetchMe, fetchProjects, getProjectId, getToken,
  setProjectId, setToken, setUnauthorizedHandler,
  type AuthUser, type Project,
} from './api/client'
import { Loader2, LogOut, FolderOpen } from 'lucide-react'
import './index.css'

type Phase = 'booting' | 'login' | 'picker' | 'app'

export default function App() {
  const [phase, setPhase] = useState<Phase>('booting')
  const [user, setUser] = useState<AuthUser | null>(null)
  const [project, setProject] = useState<Project | null>(null)
  const [view, setView] = useState<ViewKey>('chat')

  // Boot: if we have a token, hydrate the user. If we also have a saved project, jump into the app.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setUser(null)
      setProject(null)
      setPhase('login')
    })

    const token = getToken()
    if (!token) { setPhase('login'); return }

    void (async () => {
      try {
        const me = await fetchMe()
        setUser(me)
        const savedId = getProjectId()
        if (savedId != null) {
          const projects = await fetchProjects()
          const found = projects.find(p => p.id === savedId)
          if (found) {
            setProject(found)
            setPhase('app')
            return
          }
          setProjectId(null)
        }
        setPhase('picker')
      } catch {
        setToken(null)
        setProjectId(null)
        setPhase('login')
      }
    })()
  }, [])

  if (phase === 'booting') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 text-sm text-gray-500">
        <Loader2 size={16} className="animate-spin mr-2" />
        加载中…
      </div>
    )
  }

  if (phase === 'login') {
    return (
      <LoginPage
        onSuccess={u => {
          setUser(u)
          setPhase('picker')
        }}
      />
    )
  }

  if (phase === 'picker' || !project || !user) {
    return (
      <ProjectPickerPage
        user={user!}
        onPick={p => { setProject(p); setPhase('app') }}
        onLogout={() => { setUser(null); setProject(null); setPhase('login') }}
      />
    )
  }

  // Main app: chat + cases tabs, with a slim top bar showing current project + user.
  const switchProject = () => {
    setProjectId(null)
    setProject(null)
    setPhase('picker')
  }
  const logout = () => {
    setToken(null)
    setProjectId(null)
    setUser(null)
    setProject(null)
    setPhase('login')
  }

  return (
    <div className="h-screen flex flex-col">
      <header className="flex items-center justify-between px-4 py-1.5 bg-white border-b border-gray-200 text-xs flex-shrink-0">
        <div className="flex items-center gap-2 text-gray-600 min-w-0">
          <FolderOpen size={12} className="text-amber-500 flex-shrink-0" />
          <span className="font-medium text-gray-800 truncate">{project.name}</span>
          <span className="text-gray-300">·</span>
          <span className="truncate">{user.email}</span>
          {user.is_admin && <span className="text-amber-600 font-medium">[管理员]</span>}
        </div>
        <div className="flex items-center gap-3 text-gray-500 flex-shrink-0">
          <button onClick={switchProject} className="hover:text-gray-800">切换项目</button>
          <button onClick={logout} className="flex items-center gap-1 hover:text-gray-800">
            <LogOut size={11} /> 退出
          </button>
        </div>
      </header>
      <div className="flex-1 min-h-0 relative">
        <div className={view === 'chat' ? 'block h-full' : 'hidden'}>
          <ChatPage view={view} onChangeView={setView} />
        </div>
        <div className={view === 'cases' ? 'block h-full' : 'hidden'}>
          <CasesPage view={view} onChangeView={setView} />
        </div>
        <div className={view === 'knowledge' ? 'block h-full' : 'hidden'}>
          <KnowledgePage view={view} onChangeView={setView} />
        </div>
        <div className={view === 'feedback' ? 'block h-full' : 'hidden'}>
          <NegativeFeedbackPage view={view} onChangeView={setView} />
        </div>
      </div>
    </div>
  )
}
