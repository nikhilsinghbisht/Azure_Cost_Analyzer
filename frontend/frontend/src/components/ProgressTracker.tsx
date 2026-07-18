interface Props {
  messages: string[];
  isRunning: boolean;
  error?: string;
}

export default function ProgressTracker({ messages, isRunning, error }: Props) {
  if (messages.length === 0 && !isRunning) return null;

  const lastMessage = messages[messages.length - 1] ?? '';
  const isComplete = lastMessage === 'Analysis complete';

  return (
    <div className="bg-gray-800/50 border border-gray-700 rounded-xl p-5 space-y-3">
      <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
        Analysis Progress
      </p>

      <ul className="space-y-2.5">
        {messages.map((msg, i) => {
          const isLast = i === messages.length - 1;
          const isDone = !isLast || isComplete;

          return (
            <li key={i} className="flex items-center gap-3">
              {/* Icon */}
              <div className="flex-shrink-0 w-5 h-5 flex items-center justify-center">
                {isDone && !error ? (
                  <svg className="w-5 h-5 text-green-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                  </svg>
                ) : isRunning && isLast ? (
                  <svg className="w-4 h-4 text-azure animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                  </svg>
                ) : (
                  <div className="w-2 h-2 rounded-full bg-gray-600" />
                )}
              </div>

              {/* Message */}
              <span
                className={`text-sm ${
                  isLast && isRunning && !isComplete
                    ? 'text-white font-medium'
                    : isDone
                      ? 'text-gray-300'
                      : 'text-gray-500'
                }`}
              >
                {msg}
              </span>
            </li>
          );
        })}
      </ul>

      {error && (
        <p className="text-sm text-red-400 flex items-center gap-2 pt-1">
          <svg className="w-4 h-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          {error}
        </p>
      )}

      {isComplete && !error && (
        <p className="text-sm text-green-400 font-medium flex items-center gap-2 pt-1">
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          Analysis complete — redirecting to report…
        </p>
      )}
    </div>
  );
}
