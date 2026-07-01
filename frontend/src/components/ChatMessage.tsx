import ReactMarkdown from 'react-markdown'

interface Props {
  role: 'user' | 'assistant'
  content: string
  isStreaming?: boolean
}

export default function ChatMessage({ role, content, isStreaming }: Props) {
  const isUser = role === 'user'

  return (
    <div className={`flex gap-3 ${isUser ? 'justify-end' : 'justify-start'}`}>
      {!isUser && (
        <div className="w-8 h-8 rounded-full bg-blue-600 flex items-center justify-center text-white text-sm font-bold flex-shrink-0 mt-1">
          AI
        </div>
      )}

      <div
        className={`max-w-[80%] rounded-2xl px-4 py-3 text-sm leading-relaxed ${
          isUser
            ? 'bg-blue-600 text-white rounded-br-sm'
            : 'bg-white border border-gray-200 text-gray-800 rounded-bl-sm shadow-sm'
        }`}
      >
        {isUser ? (
          <p className="whitespace-pre-wrap">{content}</p>
        ) : (
          <div className="prose prose-sm max-w-none prose-pre:bg-gray-900 prose-pre:text-gray-100 prose-code:text-blue-700 prose-code:bg-blue-50 prose-code:px-1 prose-code:rounded">
            <ReactMarkdown>{content}</ReactMarkdown>
            {isStreaming && (
              <span className="inline-block w-1.5 h-4 bg-blue-500 animate-pulse ml-0.5 align-middle" />
            )}
          </div>
        )}
      </div>

      {isUser && (
        <div className="w-8 h-8 rounded-full bg-gray-300 flex items-center justify-center text-gray-600 text-sm font-bold flex-shrink-0 mt-1">
          U
        </div>
      )}
    </div>
  )
}
