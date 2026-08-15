import { useState } from 'react'
import type { ClarificationQuestion } from '../api/client'
import { CheckCircle, AlertCircle, Info, Layers, Hash } from 'lucide-react'

interface Props {
  questions: ClarificationQuestion[]
  summary: string
  suggestedModule: string
  suggestedPrefix?: string
  round?: number
  maxRounds?: number
  lockedModuleName?: string  // when present (round > 1), module input is hidden
  lockedCasePrefix?: string  // when present (round > 1), prefix input is hidden
  hideCasePrefix?: boolean   // 脑图模式：用例编号前缀无意义，隐藏该输入并从就绪校验中剔除
  confirmLabel?: string  // e.g. "继续澄清" vs "生成测试用例"
  onConfirm: (answers: Record<string, string>, moduleName: string, casePrefix: string) => void
}

const importanceIcon = {
  high: <AlertCircle size={14} className="text-red-500 flex-shrink-0" />,
  medium: <Info size={14} className="text-yellow-500 flex-shrink-0" />,
  low: <Info size={14} className="text-gray-400 flex-shrink-0" />,
}

const PREFIX_RE = /^[A-Z][A-Z0-9-]{0,39}$/

// 用户在某个问题上的选择状态：要么选中某个候选选项，要么走自定义文本输入
// selected: 选中的候选答案文本；为 null 表示用户切到"自定义"
// custom: 自定义文本框的内容（仅 selected===null 时生效）
type ChoiceState = { selected: string | null; custom: string }

export default function ClarificationPanel({
  questions, summary, suggestedModule, suggestedPrefix,
  round, maxRounds, lockedModuleName, lockedCasePrefix, hideCasePrefix, confirmLabel,
  onConfirm,
}: Props) {
  const [choices, setChoices] = useState<Record<string, ChoiceState>>({})
  const [moduleName, setModuleName] = useState(lockedModuleName ?? suggestedModule ?? '')
  const [casePrefix, setCasePrefix] = useState((lockedCasePrefix ?? suggestedPrefix ?? '').toUpperCase())

  // 取出每个问题的最终回答文本（候选 or 自定义），统一对外暴露成 Record<id,string>
  const resolveAnswer = (qid: string): string => {
    const c = choices[qid]
    if (!c) return ''
    return c.selected !== null ? c.selected : c.custom.trim()
  }
  const answers: Record<string, string> = Object.fromEntries(
    questions.map(q => [String(q.id), resolveAnswer(String(q.id))]),
  )

  // 续答轮：模块/前缀已锁定，改用紧凑信息条。脑图模式无前缀，仅凭已锁定模块名判定。
  const isFollowup = hideCasePrefix ? !!lockedModuleName : (!!lockedModuleName && !!lockedCasePrefix)

  const allAnswered = questions.every(q => answers[String(q.id)])
  const trimmedModule = moduleName.trim()
  const moduleReady = trimmedModule.length > 0
  // 脑图模式隐藏前缀 → 就绪校验不看前缀
  const prefixReady = hideCasePrefix ? true : PREFIX_RE.test(casePrefix)
  const ready = allAnswered && moduleReady && prefixReady

  return (
    <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-semibold text-amber-800 text-sm mb-1">
            {isFollowup ? `第 ${round ?? '?'} 轮澄清` : '文档解析完成'}
          </h3>
          <p className="text-xs text-amber-700">{summary}</p>
        </div>
        {round && maxRounds && (
          <span className="text-[11px] text-amber-700 bg-amber-100 px-2 py-0.5 rounded whitespace-nowrap">
            第 {round}/{maxRounds} 轮
          </span>
        )}
      </div>

      {!isFollowup && (
        <div className="bg-white rounded-lg p-3 border border-amber-100 space-y-2">
          <div className="flex items-center gap-1.5 text-xs font-medium text-amber-800">
            <Layers size={14} />
            模块名（将作为本次所有用例的统一模块）
          </div>
          <input
            className="w-full text-sm border border-gray-200 rounded-lg px-2 py-1.5 outline-none focus:border-amber-400"
            value={moduleName}
            onChange={e => setModuleName(e.target.value)}
            placeholder="请输入模块名"
          />
          {suggestedModule && (
            <p className="text-xs text-gray-400">
              大模型建议：<span className="text-gray-500">{suggestedModule}</span>
              {moduleName !== suggestedModule && (
                <button
                  onClick={() => setModuleName(suggestedModule)}
                  className="ml-2 text-blue-500 hover:underline"
                  type="button"
                >
                  采用
                </button>
              )}
            </p>
          )}
        </div>
      )}

      {!isFollowup && !hideCasePrefix && (
        <div className="bg-white rounded-lg p-3 border border-amber-100 space-y-2">
          <div className="flex items-center gap-1.5 text-xs font-medium text-amber-800">
            <Hash size={14} />
            用例编号前缀（英文大写，多个单词以短横线连接，如 USER-LOGIN）
          </div>
          <input
            className="w-full text-sm border border-gray-200 rounded-lg px-2 py-1.5 outline-none focus:border-amber-400 font-mono uppercase"
            value={casePrefix}
            onChange={e => setCasePrefix(e.target.value.toUpperCase().replace(/[^A-Z0-9-]/g, ''))}
            placeholder="USER-LOGIN"
            maxLength={40}
          />
          <p className="text-xs text-gray-400">
            所有用例编号将以 <span className="font-mono text-gray-500">{casePrefix || '<前缀>'}-</span> 开头，大模型可在前缀后再追加子场景（如 -VALID-001 / -INVALID-001）。
          </p>
          {casePrefix && !prefixReady && (
            <p className="text-xs text-red-500">必须以字母开头，仅含大写字母/数字/短横线，长度 1–40。</p>
          )}
          {suggestedPrefix && (
            <p className="text-xs text-gray-400">
              大模型建议：<span className="font-mono text-gray-500">{suggestedPrefix.toUpperCase()}</span>
              {casePrefix !== suggestedPrefix.toUpperCase() && (
                <button
                  onClick={() => setCasePrefix(suggestedPrefix.toUpperCase())}
                  className="ml-2 text-blue-500 hover:underline"
                  type="button"
                >
                  采用
                </button>
              )}
            </p>
          )}
        </div>
      )}

      {isFollowup && (
        <div className="bg-white rounded-lg p-3 border border-amber-100 text-xs text-gray-600 flex flex-wrap gap-x-4 gap-y-1">
          <span>模块：<span className="text-gray-800 font-medium">{lockedModuleName}</span></span>
          {!hideCasePrefix && lockedCasePrefix && (
            <span>前缀：<span className="font-mono text-gray-800">{lockedCasePrefix}-</span></span>
          )}
        </div>
      )}

      <div className="space-y-3">
        <p className="text-xs font-medium text-amber-800">
          在{hideCasePrefix ? '生成测试脑图' : '生成测试用例'}前，请确认以下 {questions.length} 个问题（点选候选答案，或选「自定义」自行输入）：
        </p>
        {questions.map(q => {
          const qid = String(q.id)
          const choice = choices[qid] ?? { selected: null, custom: '' }
          const opts = q.options || []
          return (
            <div key={q.id} className="bg-white rounded-lg p-3 border border-amber-100">
              <div className="flex items-start gap-2 mb-2">
                {importanceIcon[q.importance]}
                <div>
                  <span className="text-xs font-medium text-gray-600 bg-gray-100 px-1.5 py-0.5 rounded mr-2">
                    {q.category}
                  </span>
                  <p className="text-sm text-gray-800 mt-1">{q.question}</p>
                  {q.context && (
                    <p className="text-xs text-gray-400 mt-1 italic">
                      原文：「{q.context.slice(0, 80)}{q.context.length > 80 ? '…' : ''}」
                    </p>
                  )}
                </div>
              </div>

              <div className="space-y-1.5">
                {opts.map((opt, i) => {
                  const checked = choice.selected === opt
                  return (
                    <label
                      key={i}
                      className={`flex items-start gap-2 p-2 rounded-lg border text-sm cursor-pointer transition-colors ${
                        checked
                          ? 'border-amber-400 bg-amber-50'
                          : 'border-gray-200 hover:border-amber-200 hover:bg-amber-50/30'
                      }`}
                    >
                      <input
                        type="radio"
                        name={`q-${qid}`}
                        className="mt-0.5 accent-amber-600"
                        checked={checked}
                        onChange={() => setChoices(prev => ({
                          ...prev,
                          [qid]: { selected: opt, custom: prev[qid]?.custom ?? '' },
                        }))}
                      />
                      <span className="text-gray-800">{opt}</span>
                    </label>
                  )
                })}

                {/* 自定义分支：始终展示，没候选时也是唯一答案入口 */}
                <label
                  className={`flex items-start gap-2 p-2 rounded-lg border text-sm cursor-pointer transition-colors ${
                    choice.selected === null && (choice.custom || opts.length === 0)
                      ? 'border-amber-400 bg-amber-50'
                      : 'border-gray-200 hover:border-amber-200 hover:bg-amber-50/30'
                  }`}
                >
                  <input
                    type="radio"
                    name={`q-${qid}`}
                    className="mt-0.5 accent-amber-600"
                    checked={choice.selected === null}
                    onChange={() => setChoices(prev => ({
                      ...prev,
                      [qid]: { selected: null, custom: prev[qid]?.custom ?? '' },
                    }))}
                  />
                  <span className="text-gray-800 whitespace-nowrap">
                    {opts.length === 0 ? '请输入答案：' : '自定义：'}
                  </span>
                </label>
                {choice.selected === null && (
                  <textarea
                    className="w-full text-sm border border-gray-200 rounded-lg p-2 outline-none focus:border-amber-400 resize-none placeholder-gray-300"
                    rows={2}
                    placeholder="请输入您的回答…"
                    value={choice.custom}
                    onChange={e => setChoices(prev => ({
                      ...prev,
                      [qid]: { selected: null, custom: e.target.value },
                    }))}
                  />
                )}
              </div>
            </div>
          )
        })}
      </div>

      <button
        onClick={() => onConfirm(answers, trimmedModule, casePrefix)}
        disabled={!ready}
        className="w-full flex items-center justify-center gap-2 py-2 bg-amber-600 text-white text-sm rounded-lg hover:bg-amber-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
      >
        <CheckCircle size={16} />
        {!moduleReady
          ? '请填写模块名'
          : !prefixReady
            ? '请填写有效的用例编号前缀'
            : !allAnswered
              ? '请回答全部澄清问题'
              : (confirmLabel || (hideCasePrefix
                  ? `确认，按「${trimmedModule}」生成测试脑图`
                  : `确认，按「${trimmedModule}」/ ${casePrefix}- 生成测试用例`))}
      </button>
    </div>
  )
}
