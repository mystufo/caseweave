import { Check } from 'lucide-react'

// 主区顶部步骤条：按会话模式展示流程步骤，高亮当前步、已完成打勾、未到步灰显。
// currentStep 是「当前进行到的步骤索引」（0-based）；≥ steps.length 表示全部完成。
interface Props {
  mode: 'cases' | 'mindmap'
  currentStep: number
}

const CASES_STEPS = ['上传资料', '确认模块', '审核知识', '澄清', '生成用例']
const MINDMAP_STEPS = ['上传需求文档', '澄清', '生成脑图', '存入飞书']

export default function FlowSteps({ mode, currentStep }: Props) {
  const steps = mode === 'mindmap' ? MINDMAP_STEPS : CASES_STEPS
  const accent = mode === 'mindmap' ? 'indigo' : 'blue'

  return (
    <div className="flex items-center gap-1 flex-wrap">
      {steps.map((label, i) => {
        const done = i < currentStep
        const active = i === currentStep
        // 颜色用完整类名，避免 Tailwind purge 掉动态拼接的 class
        const circleCls = done
          ? (accent === 'indigo' ? 'bg-indigo-600 text-white' : 'bg-blue-600 text-white')
          : active
            ? (accent === 'indigo' ? 'bg-indigo-100 text-indigo-700 ring-2 ring-indigo-400' : 'bg-blue-100 text-blue-700 ring-2 ring-blue-400')
            : 'bg-gray-100 text-gray-400'
        const labelCls = active
          ? (accent === 'indigo' ? 'text-indigo-700 font-medium' : 'text-blue-700 font-medium')
          : done ? 'text-gray-600' : 'text-gray-400'
        return (
          <div key={label} className="flex items-center gap-1">
            <div className="flex items-center gap-1.5">
              <span className={`w-5 h-5 rounded-full flex items-center justify-center text-[11px] font-medium ${circleCls}`}>
                {done ? <Check size={12} /> : i + 1}
              </span>
              <span className={`text-xs whitespace-nowrap ${labelCls}`}>{label}</span>
            </div>
            {i < steps.length - 1 && (
              <span className={`w-5 h-px ${done ? (accent === 'indigo' ? 'bg-indigo-400' : 'bg-blue-400') : 'bg-gray-200'}`} />
            )}
          </div>
        )
      })}
    </div>
  )
}
