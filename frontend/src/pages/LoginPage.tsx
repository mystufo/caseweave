import { useState, type FormEvent } from 'react'
import { Loader2, LogIn, UserPlus } from 'lucide-react'
import { login, register, setToken, type AuthUser } from '../api/client'

interface Props {
  onSuccess: (user: AuthUser) => void
}

type Mode = 'login' | 'register'

export default function LoginPage({ onSuccess }: Props) {
  const [mode, setMode] = useState<Mode>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    if (!email.trim() || !password) {
      setError('请填写邮箱和密码')
      return
    }
    if (password.length < 6) {
      setError('密码至少 6 位')
      return
    }
    setBusy(true)
    try {
      const res = mode === 'login'
        ? await login(email.trim(), password)
        : await register(email.trim(), password, name.trim() || undefined)
      setToken(res.token)
      onSuccess(res.user)
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(detail || (mode === 'login' ? '登录失败' : '注册失败'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 px-4">
      <div className="w-full max-w-sm bg-white border border-gray-200 rounded-2xl shadow-sm p-7">
        <div className="text-center mb-5">
          <h1 className="text-lg font-semibold text-gray-900">CaseWeave 纬策</h1>
          <p className="text-xs text-gray-400 mt-1">智能测试用例生成系统</p>
        </div>

        <div className="flex bg-gray-100 rounded-lg p-1 mb-5 text-xs">
          <button
            type="button"
            onClick={() => { setMode('login'); setError(null) }}
            className={`flex-1 py-1.5 rounded-md font-medium transition ${
              mode === 'login' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-500 hover:text-gray-700'
            }`}
          >登录</button>
          <button
            type="button"
            onClick={() => { setMode('register'); setError(null) }}
            className={`flex-1 py-1.5 rounded-md font-medium transition ${
              mode === 'register' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-500 hover:text-gray-700'
            }`}
          >注册</button>
        </div>

        <form onSubmit={submit} className="space-y-3">
          <div>
            <label className="block text-[11px] font-medium text-gray-500 mb-1">邮箱</label>
            <input
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              autoComplete="email"
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none focus:border-amber-400"
              placeholder="you@example.com"
            />
          </div>
          {mode === 'register' && (
            <div>
              <label className="block text-[11px] font-medium text-gray-500 mb-1">昵称（可选）</label>
              <input
                type="text"
                value={name}
                onChange={e => setName(e.target.value)}
                maxLength={80}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none focus:border-amber-400"
              />
            </div>
          )}
          <div>
            <label className="block text-[11px] font-medium text-gray-500 mb-1">密码</label>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none focus:border-amber-400"
              placeholder="至少 6 位"
            />
          </div>

          {error && (
            <div className="text-xs text-red-600 bg-red-50 border border-red-100 rounded-md px-3 py-2">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={busy}
            className="w-full flex items-center justify-center gap-2 bg-amber-500 hover:bg-amber-600 text-white text-sm font-medium py-2 rounded-lg disabled:opacity-50"
          >
            {busy
              ? <Loader2 size={14} className="animate-spin" />
              : mode === 'login' ? <LogIn size={14} /> : <UserPlus size={14} />}
            {mode === 'login' ? '登录' : '注册'}
          </button>
        </form>

        <p className="text-[11px] text-gray-400 text-center mt-4">
          仅管理员可创建项目，普通账号注册后可加入任何项目协作。
        </p>
      </div>
    </div>
  )
}
