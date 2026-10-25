import React from 'react';

interface ScoreBadgeProps {
  score: number;
  priority?: 'High' | 'Medium' | 'Low';
  size?: 'sm' | 'md' | 'lg';
}

export const ScoreBadge: React.FC<ScoreBadgeProps> = ({ score, priority, size = 'md' }) => {
  let bgColor = 'bg-rose-500/10 text-rose-400 border-rose-500/30';
  let dotColor = 'bg-rose-400';

  if (score >= 70) {
    bgColor = 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
    dotColor = 'bg-emerald-400';
  } else if (score >= 45) {
    bgColor = 'bg-amber-500/10 text-amber-400 border-amber-500/30';
    dotColor = 'bg-amber-400';
  }

  const sizeClasses = {
    sm: 'text-xs px-2 py-0.5',
    md: 'text-sm px-2.5 py-1',
    lg: 'text-base px-3 py-1.5 font-bold',
  };

  return (
    <div className={`inline-flex items-center gap-1.5 rounded-full border font-medium ${bgColor} ${sizeClasses[size]}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${dotColor} animate-pulse`} />
      <span>{score}/100</span>
      {priority && <span className="opacity-70 text-[0.8em]">({priority})</span>}
    </div>
  );
};
