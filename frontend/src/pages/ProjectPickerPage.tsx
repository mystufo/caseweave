import { useEffect, useState, type FormEvent } from 'react'
import { Loader2, Plus, Trash2, FolderOpen, LogOut, Globe, Lock } from 'lucide-react'
import {
  createProject, deleteProject, fetchProjects, setProjectId, setToken, updateProject,
  type AuthUser, type Project,
} from '../api/client'

interface Props {
  user: AuthUser
  onPick: (project: Project) => void
  onLogout: () => void
}

export default function ProjectPickerPage({ user, onPick, onLogout }: Props) {
  const [projects, setProjects] = useState<Project[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [newPublic, setNewPublic] = useState(false)
  const [creating, setCreating] = useState(false)
  const [deletingId, setDeletingId] = useState<number | null>(null)
  const [togglingId, setTogglingId] = useState<number | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      setProjects(await fetchProjects())
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(detail || '加载项目失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const submitCreate = async (e: FormEvent) => {
    e.preventDefault()
    if (!newName.trim()) return
    setCreating(true)
    setError(null)
    try {
      const project = await createProject(newName.trim(), newDesc.trim() || undefined, newPublic)
      setNewName('')
      setNewDesc('')
      setNewPublic(false)
      setProjects(prev => [project, ...prev])
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(detail || '创建失败')
    } finally {
      setCreating(false)
    }
  }

  const remove = async (p: Project) => {
    if (!confirm(`确定删除项目「${p.name}」？该项目下的全部会话与用例都会被一并清除，无法恢复。`)) return
    setDeletingId(p.id)
    setError(null)
    try {
      await deleteProject(p.id)
      setProjects(prev => prev.filter(x => x.id !== p.id))
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(detail || '删除失败')
    } finally {
      setDeletingId(null)
    }
  }

  const toggleVisibility = async (p: Project) => {
    setTogglingId(p.id)
    setError(null)
    try {
      const updated = await updateProject(p.id, { is_public: !p.is_public })
      setProjects(prev => prev.map(x => (x.id === p.id ? updated : x)))
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(detail || '修改失败')
    } finally {
      setTogglingId(null)
    }
  }

  const pick = (p: Project) => {
    setProjectId(p.id)
    onPick(p)
  }

  // 普通账号最多创建一个自己的项目：已拥有自己的项目时隐藏新建表单
  // （列表可能含他人的公开项目，故按「是否已拥有自己的项目」判断，而非可见总数）
  const ownsProject = projects.some(p => p.creator_id === user.id)
  const canCreate = user.is_admin || !ownsProject
  // 管理员可改任意项目；普通账号只能改 / 删自己创建的项目
  const canManage = (p: Project) => user.is_admin || p.creator_id === user.id

  const logout = () => {
    setToken(null)
    setProjectId(null)
    onLogout()
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b border-gray-200">
        <div className="max-w-3xl mx-auto px-6 py-4 flex items-center justify-between">
          <div>
            <h1 className="text-base font-semibold text-gray-900">选择项目</h1>
            <p className="text-xs text-gray-400 mt-0.5">
              当前账号：<span className="font-medium text-gray-700">{user.email}</span>
              {user.is_admin && <span className="ml-2 text-amber-600 font-medium">[管理员]</span>}
            </p>
          </div>
          <button
            onClick={logout}
            className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-gray-700 px-3 py-1.5 border border-gray-200 rounded-lg"
          >
            <LogOut size={14} />
            退出登录
          </button>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-6 py-6 space-y-6">
        {error && (
          <div className="text-xs text-red-600 bg-red-50 border border-red-100 rounded-md px-3 py-2">{error}</div>
        )}

        {canCreate && (
          <section className="bg-white border border-gray-200 rounded-xl p-5">
            <h2 className="text-sm font-semibold text-gray-900 mb-3 flex items-center gap-2">
              <Plus size={14} className="text-amber-500" />
              新建项目
            </h2>
            <form onSubmit={submitCreate} className="grid grid-cols-1 md:grid-cols-[1fr_2fr_auto] gap-2 items-start">
              <input
                value={newName}
                onChange={e => setNewName(e.target.value)}
                maxLength={100}
                placeholder="项目名称（必填）"
                className="px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none focus:border-amber-400"
              />
              <input
                value={newDesc}
                onChange={e => setNewDesc(e.target.value)}
                placeholder="项目描述（可选）"
                className="px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none focus:border-amber-400"
              />
              <button
                type="submit"
                disabled={creating || !newName.trim()}
                className="flex items-center gap-1.5 bg-amber-500 hover:bg-amber-600 text-white text-sm px-4 py-2 rounded-lg disabled:opacity-50"
              >
                {creating ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />}
                创建
              </button>
            </form>
            <label className="mt-3 flex items-center gap-2 text-xs text-gray-600 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={newPublic}
                onChange={e => setNewPublic(e.target.checked)}
                className="accent-amber-500"
              />
              公开项目（所有用户可见）；不勾选则为私有，仅自己和管理员可见
            </label>
          </section>
        )}

        <section>
          <h2 className="text-sm font-semibold text-gray-900 mb-3">{user.is_admin ? '全部项目' : '项目'}</h2>
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-gray-400 py-8 justify-center">
              <Loader2 size={14} className="animate-spin" /> 加载中…
            </div>
          ) : projects.length === 0 ? (
            <div className="text-center text-sm text-gray-400 py-12 bg-white border border-dashed border-gray-200 rounded-xl">
              暂无项目。请使用上方表单创建一个。
            </div>
          ) : (
            <ul className="space-y-2">
              {projects.map(p => (
                <li
                  key={p.id}
                  className="bg-white border border-gray-200 rounded-xl px-4 py-3 flex items-center justify-between gap-3 hover:border-amber-300 transition"
                >
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-gray-900 truncate flex items-center gap-2">
                      <span className="truncate">{p.name}</span>
                      {p.is_public ? (
                        <span className="inline-flex items-center gap-1 text-[11px] text-emerald-700 bg-emerald-50 border border-emerald-100 px-1.5 py-0.5 rounded flex-shrink-0">
                          <Globe size={10} /> 公开
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-[11px] text-gray-500 bg-gray-100 border border-gray-200 px-1.5 py-0.5 rounded flex-shrink-0">
                          <Lock size={10} /> 私有
                        </span>
                      )}
                    </div>
                    {p.description && (
                      <div className="text-xs text-gray-500 mt-0.5 truncate">{p.description}</div>
                    )}
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0">
                    <button
                      onClick={() => pick(p)}
                      className="flex items-center gap-1.5 text-xs text-amber-700 bg-amber-50 hover:bg-amber-100 px-3 py-1.5 rounded-md"
                    >
                      <FolderOpen size={12} />
                      进入
                    </button>
                    {canManage(p) && (
                      <button
                        onClick={() => toggleVisibility(p)}
                        disabled={togglingId === p.id}
                        className="flex items-center gap-1.5 text-xs text-gray-600 hover:bg-gray-100 px-2 py-1.5 rounded-md disabled:opacity-50"
                        title={p.is_public ? '设为私有（仅自己和管理员可见）' : '设为公开（所有用户可见）'}
                      >
                        {togglingId === p.id
                          ? <Loader2 size={12} className="animate-spin" />
                          : p.is_public ? <Lock size={12} /> : <Globe size={12} />}
                        {p.is_public ? '设为私有' : '设为公开'}
                      </button>
                    )}
                    {canManage(p) && (
                      <button
                        onClick={() => remove(p)}
                        disabled={deletingId === p.id}
                        className="flex items-center gap-1.5 text-xs text-red-600 hover:bg-red-50 px-2 py-1.5 rounded-md disabled:opacity-50"
                        title="删除项目（不可恢复）"
                      >
                        {deletingId === p.id ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>
    </div>
  )
}
