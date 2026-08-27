import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchUsageReport, USAGE_MAX_BUCKETS,
  type UsageGranularity, type UsageReport,
} from '../api/client'
import TabBar, { type ViewKey } from '../components/TabBar'
import { Gauge, RefreshCw, Loader2, Download, Users, Activity, AlertTriangle } from 'lucide-react'

interface PageProps {
  view: ViewKey
  onChangeView: (v: ViewKey) => void
}

const GRANULARITIES: { key: UsageGranularity; label: string }[] = [
  { key: 'day', label: '按天' },
  { key: 'week', label: '按周' },
  { key: 'month', label: '按月' },
]

/** token 数缩写：12345 → 12.3k，2345678 → 2.35M。表格里列多，写全了看不过来。 */
function fmtTokens(n: number): string {
  if (!n) return '—'
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 2 : 1)}k`
  return `${(n / 1_000_000).toFixed(2)}M`
}

const fmtFull = (n: number) => n.toLocaleString('en-US')

/** 服务端给的分组起始日按 UTC 解析——它已是按 UTC+8 切好的自然日，再走本地时区会串日期。 */
const parseDay = (day: string) => new Date(`${day}T00:00:00Z`)

/** 列头：按天 '08-27'，按周 '08-24 周'，按月 '2026-08'。 */
function bucketLabel(bucket: string, gran: UsageGranularity): string {
  if (gran === 'month') return bucket.slice(0, 7)
  if (gran === 'week') return `${bucket.slice(5)} 周`
  return bucket.slice(5)
}

/** hover 提示里的完整区间描述。 */
function bucketRange(bucket: string, gran: UsageGranularity): string {
  const start = parseDay(bucket)
  if (gran === 'day') {
    const wd = start.toLocaleDateString('zh-CN', { weekday: 'long', timeZone: 'UTC' })
    return `${bucket} ${wd}`
  }
  const end = new Date(start)
  if (gran === 'week') end.setUTCDate(start.getUTCDate() + 6)
  else end.setUTCMonth(start.getUTCMonth() + 1, 0)
  return `${bucket} ~ ${end.toISOString().slice(0, 10)}`
}

/** 热力底色：按该单元格占全表峰值的比例分 4 档。 */
function heatClass(value: number, peak: number): string {
  if (!value || peak <= 0) return ''
  const r = value / peak
  if (r > 0.75) return 'bg-amber-200/90'
  if (r > 0.5) return 'bg-amber-100'
  if (r > 0.25) return 'bg-amber-50'
  return ''
}

export default function UsagePage({ view, onChangeView }: PageProps) {
  const [gran, setGran] = useState<UsageGranularity>('day')
  const [report, setReport] = useState<UsageReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    setErrorMsg(null)
    try {
      setReport(await fetchUsageReport(gran))
    } catch (err) {
      console.error('Usage load:', err)
      setErrorMsg('加载失败：需要管理员权限，或服务暂时不可用')
      setReport(null)
    } finally {
      setLoading(false)
    }
  }, [gran])

  useEffect(() => {
    void reload()
  }, [reload])

  // 与其他页面一致：本页始终挂载，切进来这一刻主动刷新一次。
  const refreshRef = useRef<() => void>(() => {})
  refreshRef.current = () => { void reload() }
  const prevViewRef = useRef<ViewKey>(view)
  useEffect(() => {
    if (view === 'usage' && prevViewRef.current !== 'usage') refreshRef.current()
    prevViewRef.current = view
  }, [view])

  // 列 = 服务端给的分组起始日（从近到远）。没有用量的分组也留列，否则趋势会被压缩看不出断档。
  const buckets = report?.buckets ?? []

  // (user_id, bucket) → 该格用量
  const cells = useMemo(() => {
    const m = new Map<string, { total: number; calls: number; input: number; output: number }>()
    for (const r of report?.by_user_period || []) {
      m.set(`${r.user_id}|${r.period}`, {
        total: r.total_tokens, calls: r.calls, input: r.input_tokens, output: r.output_tokens,
      })
    }
    return m
  }, [report])

  const periodTotals = useMemo(() => {
    const m = new Map<string, number>()
    for (const r of report?.by_period || []) m.set(r.period, r.total_tokens)
    return m
  }, [report])

  const peakCell = useMemo(() => {
    let peak = 0
    for (const v of cells.values()) peak = Math.max(peak, v.total)
    return peak
  }, [cells])

  const quota = report?.quota.daily_token_quota ?? 0
  // 配额是「每天」的，只有按天看才能拿单格和它比。
  const quotaApplies = quota > 0 && gran === 'day'

  const exportCsv = () => {
    if (!report) return
    const cols = [...buckets].reverse()
    const lines = [['账号', '姓名', ...cols, '合计', '调用次数'].join(',')]
    for (const u of report.by_user) {
      const vals = cols.map(b => String(cells.get(`${u.user_id}|${b}`)?.total ?? 0))
      lines.push([u.email, u.name || '', ...vals, String(u.total_tokens), String(u.calls)].join(','))
    }
    const blob = new Blob(['﻿' + lines.join('\n')], { type: 'text/csv;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `token-usage-${report.granularity}-${report.since}_${report.until}.csv`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  const granLabel = GRANULARITIES.find(g => g.key === gran)?.label ?? ''

  return (
    <div className="flex h-full bg-gray-50 overflow-hidden">
      <TabBar value={view} onChange={onChangeView} />

      <main className="flex-1 flex flex-col overflow-hidden">
        <header className="flex items-center gap-3 px-6 py-3 border-b border-gray-200 bg-white">
          <Gauge size={16} className="text-amber-600" />
          <h1 className="text-base font-semibold text-gray-800">Token 用量</h1>
          <span className="text-xs text-gray-400">各账号的 LLM token 消耗（管理员可见）</span>
          <div className="flex-1" />
          <div className="flex items-center gap-1.5">
            {GRANULARITIES.map(g => (
              <button
                key={g.key}
                type="button"
                onClick={() => setGran(g.key)}
                className={`px-2.5 py-1 text-xs rounded-md border transition ${
                  gran === g.key
                    ? 'bg-amber-600 text-white border-amber-600'
                    : 'bg-white text-gray-600 border-gray-200 hover:bg-amber-50'
                }`}
              >
                {g.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={exportCsv}
            disabled={!report || report.by_user.length === 0}
            className="p-1.5 text-gray-500 hover:text-gray-800 border border-gray-200 rounded-md disabled:opacity-40"
            title="导出 CSV"
          >
            <Download size={14} />
          </button>
          <button
            type="button"
            onClick={() => void reload()}
            className="p-1.5 text-gray-500 hover:text-gray-800 border border-gray-200 rounded-md"
            title="刷新"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          </button>
        </header>

        {/* 概览：区间合计 + 配额设置 + 闸门实时状态 */}
        {report && (
          <div className="px-6 py-3 border-b border-gray-200 bg-gradient-to-r from-amber-50/60 to-orange-50/40">
            <div className="flex flex-wrap items-center gap-3 text-xs">
              <div className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-white border border-amber-100">
                <Activity size={13} className="text-amber-600" />
                <span className="text-gray-500">{report.since} ~ {report.until} 合计</span>
                <span className="font-semibold text-gray-800" title={fmtFull(report.totals.total_tokens)}>
                  {fmtTokens(report.totals.total_tokens)} tokens
                </span>
                <span className="text-gray-300">·</span>
                <span className="text-gray-500">
                  输入 {fmtTokens(report.totals.input_tokens)} / 输出 {fmtTokens(report.totals.output_tokens)}
                </span>
                <span className="text-gray-300">·</span>
                <span className="text-gray-500">{fmtFull(report.totals.calls)} 次调用</span>
              </div>
              <div className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-white border border-amber-100">
                <Users size={13} className="text-amber-600" />
                <span className="text-gray-500">活跃账号</span>
                <span className="font-semibold text-gray-800">{report.totals.users}</span>
              </div>
              <div className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-white border border-amber-100">
                <span className="text-gray-500">每日配额</span>
                <span className="font-semibold text-gray-800">
                  {quota > 0 ? `${fmtFull(quota)} tokens/人/天` : '未设置（不限）'}
                </span>
                {quota > 0 && report.quota.exempt_admins && (
                  <span className="text-gray-400">· 管理员豁免</span>
                )}
                <span className="text-gray-300">·</span>
                <span className="text-gray-400">UTC+{report.quota.reset_utc_offset_hours} 零点翻篇</span>
              </div>
              <div className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-white border border-amber-100">
                <span className="text-gray-500">并发闸门</span>
                <span className="text-gray-700">
                  在跑 {report.gate.running}/{report.gate.limit}
                </span>
                <span className="text-gray-300">·</span>
                <span className={report.gate.waiting > 0 ? 'text-amber-700 font-medium' : 'text-gray-500'}>
                  排队 {report.gate.waiting}/{report.gate.queue_size}
                </span>
                <span className="text-gray-300">·</span>
                <span className="text-gray-500">单账号 {report.gate.per_user_limit}</span>
              </div>
            </div>
          </div>
        )}

        <div className="flex-1 overflow-auto px-6 py-4">
          {errorMsg && (
            <div className="px-3 py-2 rounded bg-red-50 border border-red-200 text-sm text-red-700 flex items-center gap-2">
              <AlertTriangle size={14} /> {errorMsg}
            </div>
          )}

          {loading && !report && (
            <div className="flex items-center justify-center py-16 text-sm text-gray-400">
              <Loader2 size={14} className="animate-spin mr-2" />
              加载中…
            </div>
          )}

          {report && report.by_user.length === 0 && !errorMsg && (
            <div className="flex flex-col items-center justify-center py-16 text-sm text-gray-400">
              <Gauge size={32} className="opacity-30 mb-3" />
              <div>{report.since} 以来没有任何 token 消耗记录</div>
              <div className="text-xs mt-1">生成用例、对话等 LLM 调用产生的用量会自动记在这里</div>
            </div>
          )}

          {report && report.by_user.length > 0 && (
            <div className="bg-white border border-gray-200 rounded-lg overflow-auto">
              <table className="text-xs border-collapse w-full">
                <thead>
                  <tr className="bg-gray-50 text-gray-500">
                    <th className="sticky left-0 z-10 bg-gray-50 text-left font-medium px-3 py-2 border-b border-r border-gray-200 min-w-[200px]">
                      账号
                    </th>
                    <th className="text-right font-medium px-3 py-2 border-b border-r border-gray-200 whitespace-nowrap">
                      区间合计
                    </th>
                    <th className="text-right font-medium px-3 py-2 border-b border-r border-gray-200 whitespace-nowrap">
                      调用
                    </th>
                    {buckets.map(b => (
                      <th
                        key={b}
                        title={bucketRange(b, gran)}
                        className="text-right font-medium px-2.5 py-2 border-b border-gray-200 whitespace-nowrap"
                      >
                        {bucketLabel(b, gran)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {report.by_user.map(u => (
                    <tr key={u.user_id} className="hover:bg-amber-50/40">
                      <td className="sticky left-0 z-10 bg-white px-3 py-1.5 border-b border-r border-gray-100">
                        <div className="flex items-center gap-1.5 min-w-0">
                          <span className="truncate text-gray-800" title={u.email}>{u.email}</span>
                          {u.name && <span className="text-gray-400 truncate">{u.name}</span>}
                          {u.is_admin && <span className="text-amber-600 flex-shrink-0">[管理员]</span>}
                          {quota > 0 && u.quota_exempt && (
                            <span className="text-gray-400 flex-shrink-0" title="该账号不受每日配额限制">免额</span>
                          )}
                        </div>
                      </td>
                      <td
                        className="text-right px-3 py-1.5 border-b border-r border-gray-100 font-semibold text-gray-800 tabular-nums"
                        title={`${fmtFull(u.total_tokens)} tokens（输入 ${fmtFull(u.input_tokens)} / 输出 ${fmtFull(u.output_tokens)}）`}
                      >
                        {fmtTokens(u.total_tokens)}
                      </td>
                      <td className="text-right px-3 py-1.5 border-b border-r border-gray-100 text-gray-500 tabular-nums">
                        {u.calls || '—'}
                      </td>
                      {buckets.map(b => {
                        const c = cells.get(`${u.user_id}|${b}`)
                        const v = c?.total ?? 0
                        const over = quotaApplies && !u.quota_exempt && v >= quota
                        return (
                          <td
                            key={b}
                            title={
                              c
                                ? `${u.email} · ${bucketRange(b, gran)}\n${fmtFull(v)} tokens（输入 ${fmtFull(c.input)} / 输出 ${fmtFull(c.output)}）\n${c.calls} 次调用${over ? '\n已触达当日配额' : ''}`
                                : `${u.email} · ${bucketRange(b, gran)}\n无用量`
                            }
                            className={`text-right px-2.5 py-1.5 border-b border-gray-100 tabular-nums ${
                              over ? 'bg-rose-100 text-rose-700 font-medium' : `${heatClass(v, peakCell)} text-gray-600`
                            }`}
                          >
                            {fmtTokens(v)}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="bg-gray-50 text-gray-700 font-medium">
                    <td className="sticky left-0 z-10 bg-gray-50 px-3 py-2 border-t border-r border-gray-200">
                      全站合计
                    </td>
                    <td
                      className="text-right px-3 py-2 border-t border-r border-gray-200 tabular-nums"
                      title={fmtFull(report.totals.total_tokens)}
                    >
                      {fmtTokens(report.totals.total_tokens)}
                    </td>
                    <td className="text-right px-3 py-2 border-t border-r border-gray-200 tabular-nums">
                      {report.totals.calls}
                    </td>
                    {buckets.map(b => (
                      <td
                        key={b}
                        className="text-right px-2.5 py-2 border-t border-gray-200 tabular-nums"
                        title={`${bucketRange(b, gran)} 全站合计 ${fmtFull(periodTotals.get(b) ?? 0)} tokens`}
                      >
                        {fmtTokens(periodTotals.get(b) ?? 0)}
                      </td>
                    ))}
                  </tr>
                </tfoot>
              </table>
            </div>
          )}

          {report && report.by_user.length > 0 && (
            <div className="mt-2 text-[11px] text-gray-400">
              {granLabel}分组，最多 {USAGE_MAX_BUCKETS} 组，从近到远
              {gran === 'day' && '（自然日按 UTC+' + report.quota.reset_utc_offset_hours + '，即北京时间零点翻篇）'}
              {gran === 'week' && '（每周一为界）'}
              {gran === 'month' && '（每月 1 号为界）'}
              ；行按区间总消耗从高到低排序，底色越深表示该格消耗越多
              {quotaApplies ? '，红色表示该账号当天已触达每日配额' : ''}。
              「调用」是 LLM 调用次数，一次用例生成通常包含多次调用。
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
