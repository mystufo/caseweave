import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import axios from 'axios'
import {
  fetchSystemSettings, saveSystemSettings, testSystemSettings,
  type SettingField, type SettingGroup, type SettingsTestResult, type SystemSettings,
} from '../api/client'
import TabBar, { type ViewKey } from '../components/TabBar'
import { toast } from '../lib/toast'
import {
  SlidersHorizontal, RefreshCw, Loader2, Save, PlugZap, Undo2, AlertTriangle,
  CheckCircle2, XCircle, Eye, EyeOff,
} from 'lucide-react'

interface PageProps {
  view: ViewKey
  onChangeView: (v: ViewKey) => void
}

type Draft = Record<string, string | boolean>

const SOURCE_BADGE: Record<SettingField['source'], { text: string; cls: string }> = {
  db: { text: '页面已覆盖', cls: 'bg-amber-100 text-amber-800 border-amber-200' },
  env: { text: '来自 .env', cls: 'bg-gray-100 text-gray-600 border-gray-200' },
  default: { text: '默认值', cls: 'bg-gray-50 text-gray-400 border-gray-200' },
}

/** 表单里的值统一存字符串（bool 除外）；密钥不回填，空串 = 不修改。 */
function toDraft(f: SettingField): string | boolean {
  if (f.kind === 'bool') return Boolean(f.value)
  if (f.secret) return ''
  return f.value === null || f.value === undefined ? '' : String(f.value)
}

function isDirty(f: SettingField, d: string | boolean | undefined): boolean {
  if (d === undefined) return false
  if (f.kind === 'bool') return Boolean(d) !== Boolean(f.value)
  if (f.secret) return String(d).trim() !== ''
  return String(d) !== String(f.value ?? '')
}

function fmtBaseline(f: SettingField): string {
  if (f.kind === 'bool') return f.baseline ? '开' : '关'
  if (f.kind === 'select') {
    const o = f.options.find(x => x.value === String(f.baseline ?? ''))
    return o?.label ?? String(f.baseline)
  }
  const s = String(f.baseline ?? '')
  return s === '' ? '（空）' : s
}

export default function SettingsPage({ view, onChangeView }: PageProps) {
  const [data, setData] = useState<SystemSettings | null>(null)
  const [loading, setLoading] = useState(false)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft>({})
  const [resetKeys, setResetKeys] = useState<Set<string>>(new Set())
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState<string | null>(null)   // group key
  const [testing, setTesting] = useState<string | null>(null)  // group key
  const [testResult, setTestResult] = useState<Record<string, SettingsTestResult>>({})
  const [reveal, setReveal] = useState<Set<string>>(new Set())

  const applySnapshot = useCallback((snap: SystemSettings) => {
    setData(snap)
    const d: Draft = {}
    for (const g of snap.groups) for (const f of g.fields) d[f.key] = toDraft(f)
    setDraft(d)
    setResetKeys(new Set())
    setFieldErrors({})
  }, [])

  const reload = useCallback(async () => {
    setLoading(true)
    setErrorMsg(null)
    try {
      applySnapshot(await fetchSystemSettings())
    } catch (err) {
      console.error('Settings load:', err)
      setErrorMsg('加载失败：需要管理员权限，或服务暂时不可用')
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [applySnapshot])

  useEffect(() => { void reload() }, [reload])

  // 与其他页面一致：本页始终挂载，切进来这一刻刷新一次（但有未保存改动时不刷，免得吞掉输入）。
  const refreshRef = useRef<() => void>(() => {})
  const hasAnyDirty = useMemo(() => {
    if (!data) return false
    if (resetKeys.size) return true
    return data.groups.some(g => g.fields.some(f => isDirty(f, draft[f.key])))
  }, [data, draft, resetKeys])
  refreshRef.current = () => { if (!hasAnyDirty) void reload() }
  const prevViewRef = useRef<ViewKey>(view)
  useEffect(() => {
    if (view === 'settings' && prevViewRef.current !== 'settings') refreshRef.current()
    prevViewRef.current = view
  }, [view])

  const setValue = (key: string, v: string | boolean) => {
    setDraft(prev => ({ ...prev, [key]: v }))
    setFieldErrors(prev => { if (!prev[key]) return prev; const n = { ...prev }; delete n[key]; return n })
    // 改了值就不再算「恢复」
    setResetKeys(prev => { if (!prev.has(key)) return prev; const n = new Set(prev); n.delete(key); return n })
  }

  const toggleReset = (f: SettingField) => {
    setResetKeys(prev => {
      const n = new Set(prev)
      if (n.has(f.key)) n.delete(f.key)
      else n.add(f.key)
      return n
    })
    setDraft(prev => ({ ...prev, [f.key]: toDraft(f) }))
  }

  /** 该组要提交的 values / reset。 */
  const collect = (g: SettingGroup) => {
    const values: Record<string, unknown> = {}
    const reset: string[] = []
    for (const f of g.fields) {
      if (resetKeys.has(f.key)) { reset.push(f.key); continue }
      if (isDirty(f, draft[f.key])) values[f.key] = draft[f.key]
    }
    return { values, reset }
  }

  const handleApiError = (err: unknown, fallback: string) => {
    if (axios.isAxiosError(err) && err.response?.status === 422) {
      const errors = (err.response.data as { detail?: { errors?: Record<string, string> } })?.detail?.errors
      if (errors) {
        setFieldErrors(errors)
        toast.error(errors._ ?? '有字段填写不合法，请检查标红项')
        return
      }
    }
    console.error(fallback, err)
    toast.error(fallback)
  }

  const save = async (g: SettingGroup) => {
    const { values, reset } = collect(g)
    if (!Object.keys(values).length && !reset.length) return
    setSaving(g.key)
    try {
      const snap = await saveSystemSettings(values, reset)
      // 只回填当前组：其它组可能还有没保存的输入
      const otherDraft: Draft = {}
      for (const og of (data?.groups ?? [])) {
        if (og.key === g.key) continue
        for (const f of og.fields) otherDraft[f.key] = draft[f.key]
      }
      applySnapshot(snap)
      setDraft(prev => ({ ...prev, ...otherDraft }))
      setResetKeys(prev => { const n = new Set(prev); for (const f of g.fields) n.delete(f.key); return n })
      setTestResult(prev => { const n = { ...prev }; delete n[g.key]; return n })
      toast.success(`${g.label} 已保存并生效`)
    } catch (err) {
      handleApiError(err, '保存失败')
    } finally {
      setSaving(null)
    }
  }

  const discard = (g: SettingGroup) => {
    setDraft(prev => {
      const n = { ...prev }
      for (const f of g.fields) n[f.key] = toDraft(f)
      return n
    })
    setResetKeys(prev => { const n = new Set(prev); for (const f of g.fields) n.delete(f.key); return n })
    setFieldErrors(prev => { const n = { ...prev }; for (const f of g.fields) delete n[f.key]; return n })
  }

  const test = async (g: SettingGroup) => {
    // 带上屏幕上的值（含未保存的）；标了「恢复」的键不传，服务端用当前生效值
    const values: Record<string, unknown> = {}
    for (const f of g.fields) {
      if (resetKeys.has(f.key)) continue
      const d = draft[f.key]
      if (f.secret && !String(d ?? '').trim()) continue
      values[f.key] = d
    }
    setTesting(g.key)
    setTestResult(prev => { const n = { ...prev }; delete n[g.key]; return n })
    try {
      const r = await testSystemSettings(g.key, values)
      setTestResult(prev => ({ ...prev, [g.key]: r }))
    } catch (err) {
      handleApiError(err, '测试请求失败')
    } finally {
      setTesting(null)
    }
  }

  const renderInput = (f: SettingField) => {
    const d = draft[f.key]
    const err = fieldErrors[f.key]
    const base = `w-full text-sm border rounded-md px-2.5 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-amber-200 ${
      err ? 'border-red-400' : 'border-gray-200'
    }`
    if (f.kind === 'bool') {
      const on = Boolean(d)
      return (
        <button
          type="button"
          role="switch"
          aria-checked={on}
          onClick={() => setValue(f.key, !on)}
          className={`relative inline-flex h-6 w-11 items-center rounded-full transition ${on ? 'bg-amber-500' : 'bg-gray-300'}`}
        >
          <span className={`inline-block h-5 w-5 rounded-full bg-white shadow transform transition ${on ? 'translate-x-5' : 'translate-x-0.5'}`} />
        </button>
      )
    }
    if (f.kind === 'select') {
      return (
        <select value={String(d ?? '')} onChange={e => setValue(f.key, e.target.value)} className={base}>
          {f.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      )
    }
    if (f.kind === 'int' || f.kind === 'float') {
      return (
        <input
          type="number"
          value={String(d ?? '')}
          min={f.min ?? undefined}
          max={f.max ?? undefined}
          step={f.kind === 'int' ? 1 : 'any'}
          onChange={e => setValue(f.key, e.target.value)}
          className={base}
        />
      )
    }
    if (f.secret) {
      const shown = reveal.has(f.key)
      const current = String(f.value || '')
      return (
        <div className="relative">
          <input
            type={shown ? 'text' : 'password'}
            value={String(d ?? '')}
            autoComplete="new-password"
            placeholder={current ? `已配置 ${current}，留空不修改` : '未配置'}
            onChange={e => setValue(f.key, e.target.value)}
            className={`${base} pr-9`}
          />
          <button
            type="button"
            onClick={() => setReveal(prev => { const n = new Set(prev); if (n.has(f.key)) n.delete(f.key); else n.add(f.key); return n })}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-700"
            title={shown ? '隐藏' : '显示输入'}
          >
            {shown ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
        </div>
      )
    }
    return (
      <input
        type="text"
        value={String(d ?? '')}
        placeholder={f.placeholder}
        onChange={e => setValue(f.key, e.target.value)}
        className={base}
      />
    )
  }

  const renderField = (f: SettingField) => {
    const dirty = isDirty(f, draft[f.key])
    const pendingReset = resetKeys.has(f.key)
    const badge = SOURCE_BADGE[f.source]
    const err = fieldErrors[f.key]
    return (
      <div
        key={f.key}
        className={`rounded-lg border p-3 bg-white ${
          pendingReset ? 'border-blue-200 bg-blue-50/40' : dirty ? 'border-amber-300' : 'border-gray-200'
        }`}
      >
        <div className="flex items-center gap-2 mb-1.5">
          <label className="text-sm font-medium text-gray-800">{f.label}</label>
          <span className={`text-[10px] px-1.5 py-0.5 rounded border ${badge.cls}`}>{badge.text}</span>
          {dirty && !pendingReset && (
            <span className="text-[10px] px-1.5 py-0.5 rounded border bg-amber-50 text-amber-700 border-amber-200">未保存</span>
          )}
          {pendingReset && (
            <span className="text-[10px] px-1.5 py-0.5 rounded border bg-blue-50 text-blue-700 border-blue-200">
              保存后恢复为 {fmtBaseline(f)}
            </span>
          )}
          <div className="flex-1" />
          {f.source === 'db' && (
            <button
              type="button"
              onClick={() => toggleReset(f)}
              className={`inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded border transition ${
                pendingReset
                  ? 'bg-blue-600 text-white border-blue-600'
                  : 'text-gray-500 border-gray-200 hover:bg-gray-50 hover:text-gray-800'
              }`}
              title={`删掉页面覆盖，恢复为 .env / 默认值：${fmtBaseline(f)}`}
            >
              <Undo2 size={11} /> {pendingReset ? '取消恢复' : '恢复 .env'}
            </button>
          )}
        </div>
        <div className={pendingReset ? 'opacity-50 pointer-events-none' : ''}>{renderInput(f)}</div>
        {err
          ? <p className="mt-1 text-xs text-red-600">{err}</p>
          : f.help && <p className="mt-1 text-xs text-gray-400 leading-snug">{f.help}</p>}
      </div>
    )
  }

  const renderGroup = (g: SettingGroup) => {
    const { values, reset } = collect(g)
    const groupDirty = Object.keys(values).length > 0 || reset.length > 0
    const r = testResult[g.key]
    return (
      <section key={g.key} className="rounded-xl border border-gray-200 bg-gray-50/60 overflow-hidden">
        <header className="px-4 py-3 border-b border-gray-200 bg-white flex items-center gap-3">
          <div>
            <h2 className="text-sm font-semibold text-gray-800">{g.label}</h2>
            <p className="text-xs text-gray-400 mt-0.5">{g.desc}</p>
          </div>
          <div className="flex-1" />
          <button
            type="button"
            onClick={() => void test(g)}
            disabled={testing !== null || saving !== null}
            className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-md border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            title="用当前表单里的值发一次最小请求，不会保存"
          >
            {testing === g.key ? <Loader2 size={13} className="animate-spin" /> : <PlugZap size={13} />}
            测试连接
          </button>
          {groupDirty && (
            <button
              type="button"
              onClick={() => discard(g)}
              disabled={saving !== null}
              className="px-2.5 py-1.5 text-xs rounded-md border border-gray-200 bg-white text-gray-600 hover:bg-gray-50 disabled:opacity-50"
            >
              放弃修改
            </button>
          )}
          <button
            type="button"
            onClick={() => void save(g)}
            disabled={!groupDirty || saving !== null}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-40"
          >
            {saving === g.key ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
            保存并生效
          </button>
        </header>

        {r && (
          <div className={`px-4 py-2 text-xs border-b flex items-start gap-2 ${
            r.ok ? 'bg-emerald-50 border-emerald-100 text-emerald-800' : 'bg-red-50 border-red-100 text-red-800'
          }`}>
            {r.ok ? <CheckCircle2 size={14} className="mt-0.5 flex-shrink-0" /> : <XCircle size={14} className="mt-0.5 flex-shrink-0" />}
            <div className="min-w-0">
              {r.ok ? (
                <>
                  <span className="font-medium">连接成功</span>
                  <span className="text-emerald-700/80"> · {r.model} · {r.latency_ms} ms</span>
                  {r.reply && <span className="text-emerald-700/80"> · 回复：「{r.reply}」</span>}
                </>
              ) : (
                <>
                  <span className="font-medium">连接失败</span>
                  <span> · {r.model}{r.latency_ms != null ? ` · ${r.latency_ms} ms` : ''}</span>
                  <pre className="mt-1 whitespace-pre-wrap break-all font-mono text-[11px] text-red-700">{r.error}</pre>
                </>
              )}
            </div>
          </div>
        )}

        <div className="p-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {g.fields.map(renderField)}
        </div>
      </section>
    )
  }

  return (
    <div className="flex h-full bg-gray-50 overflow-hidden">
      <TabBar value={view} onChange={onChangeView} />

      <main className="flex-1 flex flex-col overflow-hidden">
        <header className="flex items-center gap-3 px-6 py-3 border-b border-gray-200 bg-white">
          <SlidersHorizontal size={16} className="text-amber-600" />
          <h1 className="text-base font-semibold text-gray-800">系统设置</h1>
          <span className="text-xs text-gray-400">在网页上覆盖 .env 的模型配置，保存即生效（管理员可见）</span>
          <div className="flex-1" />
          <button
            type="button"
            onClick={() => void reload()}
            disabled={hasAnyDirty}
            className="p-1.5 text-gray-500 hover:text-gray-800 border border-gray-200 rounded-md disabled:opacity-40"
            title={hasAnyDirty ? '有未保存的修改，先保存或放弃' : '刷新'}
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {data?.secrets_volatile && (
            <div className="flex items-start gap-2 text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
              <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
              <span>
                服务端未配置 <code>JWT_SECRET</code>，此处保存的 API Key 用临时密钥加密，<b>后端重启后将失效并回退到 .env</b>。
                生产环境请在 .env 设置 JWT_SECRET 后再在这里保存密钥。
              </span>
            </div>
          )}
          <div className="text-xs text-gray-500 bg-white border border-gray-200 rounded-lg px-3 py-2">
            优先级：<b>页面覆盖 &gt; .env &gt; 默认值</b>。这里只开放不需要重启就能生效的项；数据库、JWT、管理员名单、并发闸门、
            Embedding 维度等仍在 .env 里改。
          </div>

          {errorMsg && (
            <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{errorMsg}</div>
          )}
          {!data && loading && (
            <div className="flex items-center gap-2 text-sm text-gray-400 py-10 justify-center">
              <Loader2 size={16} className="animate-spin" /> 加载中…
            </div>
          )}
          {data?.groups.map(renderGroup)}
        </div>
      </main>
    </div>
  )
}
