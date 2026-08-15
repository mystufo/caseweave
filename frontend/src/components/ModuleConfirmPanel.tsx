import { Layers, Check, X, Loader2 } from 'lucide-react'
import type { ModuleSummary } from '../api/client'

interface Props {
  modules: ModuleSummary[]
  // LLM 命中的既有模块（高置信自动落库 或 中置信建议），用于文案与默认选中
  suggestedModuleId: number | null
  suggestedModuleName: string | null
  applied: boolean          // 后端是否已高置信自动落库（仅影响文案）
  confidence: number
  reasoning: string
  // 当前下拉选中的模块 id（null = 不归入模块 / 项目级）
  selectedModuleId: number | null
  // 选择「新建模块」时展开的可编辑字段
  createNew: boolean
  createName: string
  createCode: string
  createDescription: string | null
  creating: boolean
  onPatch: (patch: Partial<{
    selectedModuleId: number | null
    createNew: boolean
    createName: string
    createCode: string
    createDescription: string | null
  }>) => void
  onConfirm: () => void
  onDismiss: () => void
}

// 下拉里除了各模块，还有两个特殊项
const OPT_NONE = '__none__'   // 不归入任何模块（项目级）
const OPT_NEW = '__new__'     // 新建模块

export default function ModuleConfirmPanel({
  modules, suggestedModuleId, suggestedModuleName, applied, confidence, reasoning,
  selectedModuleId, createNew, createName, createCode, createDescription, creating,
  onPatch, onConfirm, onDismiss,
}: Props) {
  const pct = Math.round((confidence || 0) * 100)
  const codeOk = createCode.trim() === '' || /^[A-Z][A-Z0-9-]{0,39}$/.test(createCode.trim())
  const nameOk = !createNew || createName.trim().length > 0
  const canConfirm = nameOk && codeOk && !creating

  // 下拉当前值：新建分支 → OPT_NEW；否则按选中 id / 无模块
  const selectValue = createNew
    ? OPT_NEW
    : selectedModuleId != null ? String(selectedModuleId) : OPT_NONE

  const handleSelect = (v: string) => {
    if (v === OPT_NEW) onPatch({ createNew: true })
    else if (v === OPT_NONE) onPatch({ createNew: false, selectedModuleId: null })
    else onPatch({ createNew: false, selectedModuleId: Number(v) })
  }

  // 头部文案：区分「高置信自动命中」「有建议」「无建议」
  const headline = applied && suggestedModuleName
    ? `已自动归类到模块「${suggestedModuleName}」，请确认或调整`
    : suggestedModuleName
      ? `建议归类到模块「${suggestedModuleName}」，请确认或调整`
      : '请确认这份文档归属的模块'

  return (
    <div className="bg-teal-50/70 border border-teal-200 rounded-xl p-3 space-y-3">
      <div className="flex items-center gap-2 text-sm font-medium text-teal-800">
        <Layers size={15} />
        确认归属模块
        <span className="text-xs font-normal text-teal-600/80">{headline}</span>
      </div>

      {(reasoning || pct > 0) && (
        <div className="text-[11px] text-teal-700/80">
          {pct > 0 && <span>置信度 {pct}%。</span>}
          {reasoning && <span>理由：{reasoning}</span>}
        </div>
      )}

      <label className="text-xs text-gray-600 space-y-1 block">
        <span>加入哪个模块</span>
        <select
          value={selectValue}
          onChange={e => handleSelect(e.target.value)}
          disabled={creating}
          className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded bg-white focus:outline-none focus:ring-2 focus:ring-teal-200"
        >
          {modules.map(m => (
            <option key={m.id} value={String(m.id)}>
              {m.name}{m.code ? `（${m.code}）` : ''}
              {m.id === suggestedModuleId ? ' · 推荐' : ''}
            </option>
          ))}
          <option value={OPT_NONE}>不归入任何模块（项目级）</option>
          <option value={OPT_NEW}>+ 新建模块…</option>
        </select>
      </label>

      {createNew && (
        <div className="space-y-2 rounded-lg border border-teal-200 bg-white/60 p-2.5">
          <div className="grid grid-cols-2 gap-2">
            <label className="text-xs text-gray-600 space-y-1">
              <span>模块名</span>
              <input
                type="text"
                value={createName}
                onChange={e => onPatch({ createName: e.target.value })}
                disabled={creating}
                placeholder="如：订单管理"
                className="w-full px-2 py-1 text-sm border border-gray-200 rounded focus:outline-none focus:ring-2 focus:ring-teal-200"
              />
            </label>
            <label className="text-xs text-gray-600 space-y-1">
              <span>英文名（用例编号前缀）</span>
              <input
                type="text"
                value={createCode}
                onChange={e => onPatch({ createCode: e.target.value.toUpperCase() })}
                disabled={creating}
                placeholder="如：ORDER-MGMT"
                className={`w-full px-2 py-1 text-sm border rounded font-mono focus:outline-none focus:ring-2 ${
                  codeOk ? 'border-gray-200 focus:ring-teal-200' : 'border-red-300 focus:ring-red-200'
                }`}
              />
            </label>
          </div>
          {!codeOk && (
            <div className="text-[11px] text-red-500">英文名须为大写字母开头，仅含 A–Z 0–9 和短横线（如 ORDER-MGMT）</div>
          )}
          <label className="text-xs text-gray-600 space-y-1 block">
            <span>描述（可选）</span>
            <input
              type="text"
              value={createDescription || ''}
              onChange={e => onPatch({ createDescription: e.target.value || null })}
              disabled={creating}
              placeholder="一句话概括该模块职责"
              className="w-full px-2 py-1 text-sm border border-gray-200 rounded focus:outline-none focus:ring-2 focus:ring-teal-200"
            />
          </label>
        </div>
      )}

      <div className="flex items-center gap-2 pt-1">
        <button
          type="button"
          onClick={onConfirm}
          disabled={!canConfirm}
          className="inline-flex items-center gap-1 px-3 py-1.5 text-sm bg-teal-600 text-white rounded-md hover:bg-teal-700 disabled:opacity-50"
        >
          {creating ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
          {createNew ? '创建并归类' : '确认归类'}
        </button>
        <button
          type="button"
          onClick={onDismiss}
          disabled={creating}
          className="inline-flex items-center gap-1 px-3 py-1.5 text-sm text-gray-500 border border-gray-200 rounded-md hover:bg-gray-50 disabled:opacity-50"
        >
          <X size={14} />
          忽略
        </button>
      </div>
    </div>
  )
}
