import { useState } from 'react'
import { ThumbsUp, ThumbsDown, Download, Edit2, Check, X, Trash2 } from 'lucide-react'
import type { TestCase } from '../api/client'
import { submitFeedback, deleteTestCase } from '../api/client'

interface Props {
  cases: TestCase[]
  onExport: () => void
  onCaseUpdate?: (id: number, patch: Partial<TestCase>) => void
  onCaseDelete?: (id: number) => void
}

const PRIORITY_STYLE: Record<'P1' | 'P2' | 'P3', string> = {
  P1: 'bg-red-100 text-red-700 border-red-200',
  P2: 'bg-amber-100 text-amber-700 border-amber-200',
  P3: 'bg-gray-100 text-gray-600 border-gray-200',
}

function PriorityBadge({ value }: { value: TestCase['priority'] }) {
  const v = (value || 'P2') as 'P1' | 'P2' | 'P3'
  return (
    <span className={`inline-block px-1.5 py-0.5 rounded border text-[11px] font-mono font-semibold ${PRIORITY_STYLE[v]}`}>
      {v}
    </span>
  )
}

export default function TestCaseTable({ cases, onExport, onCaseUpdate, onCaseDelete }: Props) {
  const [feedback, setFeedback] = useState<Record<number, 'like' | 'dislike'>>({})
  const [editing, setEditing] = useState<number | null>(null)
  const [editData, setEditData] = useState<Partial<TestCase>>({})
  // 二次确认：第一次点删除展开内联确认条，第二次才真删——避免误触
  const [confirmingDelete, setConfirmingDelete] = useState<number | null>(null)
  const [deleting, setDeleting] = useState<number | null>(null)

  const performDelete = async (id: number) => {
    setDeleting(id)
    try {
      await deleteTestCase(id)
      onCaseDelete?.(id)
    } catch (err) {
      console.error('Delete case failed:', err)
      alert('删除失败，请重试')
    } finally {
      setDeleting(null)
      setConfirmingDelete(null)
    }
  }

  const handleFeedback = async (id: number, type: 'like' | 'dislike') => {
    setFeedback(prev => ({ ...prev, [id]: type }))
    await submitFeedback(id, type)
  }

  const startEdit = (tc: TestCase) => {
    setEditing(tc.id)
    setEditData({
      name: tc.name,
      priority: tc.priority || 'P2',
      preconditions: tc.preconditions,
      steps: tc.steps,
      expected_result: tc.expected_result,
      remarks: tc.remarks,
    })
  }

  const saveEdit = async (tc: TestCase) => {
    const original = {
      name: tc.name,
      priority: tc.priority || 'P2',
      preconditions: tc.preconditions,
      steps: tc.steps,
      expected_result: tc.expected_result,
      remarks: tc.remarks,
    }
    await submitFeedback(tc.id, 'edit', editData as Record<string, string>, original)
    onCaseUpdate?.(tc.id, editData)
    setEditing(null)
  }

  if (cases.length === 0) return null

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-gray-700 text-sm">
          已生成 {cases.length} 条测试用例
        </h3>
        <button
          onClick={onExport}
          className="flex items-center gap-1.5 text-xs px-3 py-1.5 bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors"
        >
          <Download size={14} />
          导出 Excel
        </button>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs border-collapse">
          <thead>
            <tr className="bg-gray-700 text-white">
              <th className="px-2 py-2 text-left font-medium w-28">用例编号</th>
              <th className="px-2 py-2 text-left font-medium">用例名称</th>
              <th className="px-2 py-2 text-left font-medium w-24">模块</th>
              <th className="px-2 py-2 text-center font-medium w-16">优先级</th>
              <th className="px-2 py-2 text-left font-medium">前置条件</th>
              <th className="px-2 py-2 text-left font-medium">执行步骤</th>
              <th className="px-2 py-2 text-left font-medium">预期结果</th>
              <th className="px-2 py-2 text-center font-medium w-20">操作</th>
            </tr>
          </thead>
          <tbody>
            {cases.map((tc, idx) => {
              const isEditing = editing === tc.id
              const rowBg = idx % 2 === 0 ? 'bg-white' : 'bg-gray-50'

              return (
                <tr key={tc.id} className={`${rowBg} border-b border-gray-100 hover:bg-blue-50/30 transition-colors`}>
                  <td className="px-2 py-2 font-mono text-blue-700 whitespace-nowrap">{tc.case_number}</td>
                  <td className="px-2 py-2">
                    {isEditing ? (
                      <input
                        className="w-full border rounded px-1 py-0.5 text-xs"
                        value={editData.name || ''}
                        onChange={e => setEditData(p => ({ ...p, name: e.target.value }))}
                      />
                    ) : (
                      <span>{tc.name}</span>
                    )}
                  </td>
                  <td className="px-2 py-2 text-gray-500">{tc.module}</td>
                  <td className="px-2 py-2 text-center">
                    {isEditing ? (
                      <select
                        className="border rounded px-1 py-0.5 text-xs"
                        value={editData.priority || 'P2'}
                        onChange={e => setEditData(p => ({ ...p, priority: e.target.value as 'P1' | 'P2' | 'P3' }))}
                      >
                        <option value="P1">P1</option>
                        <option value="P2">P2</option>
                        <option value="P3">P3</option>
                      </select>
                    ) : (
                      <PriorityBadge value={tc.priority} />
                    )}
                  </td>
                  <td className="px-2 py-2 text-gray-600 max-w-[160px]">
                    {isEditing ? (
                      <textarea
                        className="w-full border rounded px-1 py-0.5 text-xs resize-none"
                        rows={2}
                        value={editData.preconditions || ''}
                        onChange={e => setEditData(p => ({ ...p, preconditions: e.target.value }))}
                      />
                    ) : (
                      <span className="whitespace-pre-line">{tc.preconditions}</span>
                    )}
                  </td>
                  <td className="px-2 py-2 max-w-[200px]">
                    {isEditing ? (
                      <textarea
                        className="w-full border rounded px-1 py-0.5 text-xs resize-none"
                        rows={3}
                        value={editData.steps || ''}
                        onChange={e => setEditData(p => ({ ...p, steps: e.target.value }))}
                      />
                    ) : (
                      <span className="whitespace-pre-line">{tc.steps}</span>
                    )}
                  </td>
                  <td className="px-2 py-2 max-w-[200px]">
                    {isEditing ? (
                      <textarea
                        className="w-full border rounded px-1 py-0.5 text-xs resize-none"
                        rows={3}
                        value={editData.expected_result || ''}
                        onChange={e => setEditData(p => ({ ...p, expected_result: e.target.value }))}
                      />
                    ) : (
                      <span className="whitespace-pre-line">{tc.expected_result}</span>
                    )}
                  </td>
                  <td className="px-2 py-2">
                    <div className="flex items-center justify-center gap-1">
                      {isEditing ? (
                        <>
                          <button onClick={() => saveEdit(tc)} className="text-green-600 hover:text-green-700">
                            <Check size={14} />
                          </button>
                          <button onClick={() => setEditing(null)} className="text-red-400 hover:text-red-500">
                            <X size={14} />
                          </button>
                        </>
                      ) : confirmingDelete === tc.id ? (
                        <>
                          <span className="text-[11px] text-red-600 mr-1">确认删除？</span>
                          <button
                            onClick={() => performDelete(tc.id)}
                            disabled={deleting === tc.id}
                            className="text-red-600 hover:text-red-700 disabled:opacity-50"
                            title="确认删除"
                          >
                            <Check size={14} />
                          </button>
                          <button
                            onClick={() => setConfirmingDelete(null)}
                            disabled={deleting === tc.id}
                            className="text-gray-400 hover:text-gray-500 disabled:opacity-50"
                            title="取消"
                          >
                            <X size={14} />
                          </button>
                        </>
                      ) : (
                        <>
                          <button
                            onClick={() => handleFeedback(tc.id, 'like')}
                            className={`transition-colors ${feedback[tc.id] === 'like' ? 'text-green-600' : 'text-gray-300 hover:text-green-500'}`}
                            title="赞"
                          >
                            <ThumbsUp size={13} />
                          </button>
                          <button
                            onClick={() => handleFeedback(tc.id, 'dislike')}
                            className={`transition-colors ${feedback[tc.id] === 'dislike' ? 'text-red-500' : 'text-gray-300 hover:text-red-400'}`}
                            title="踩"
                          >
                            <ThumbsDown size={13} />
                          </button>
                          <button
                            onClick={() => startEdit(tc)}
                            className="text-gray-300 hover:text-blue-500 transition-colors"
                            title="编辑"
                          >
                            <Edit2 size={13} />
                          </button>
                          <button
                            onClick={() => setConfirmingDelete(tc.id)}
                            className="text-gray-300 hover:text-red-500 transition-colors"
                            title="删除"
                          >
                            <Trash2 size={13} />
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
