"use client";

import React, { useState } from "react";
import {
  HelpCircle,
  CheckCircle2,
  Check,
  Loader2,
  Send,
} from "lucide-react";

export interface ClarificationPromptCardProps {
  question: string;
  options: string[];
  index?: number;
  total?: number;
  isPending: boolean;
  selectedAnswer?: string | null;
  onSelectOption?: (option: string) => Promise<void> | void;
  disabled?: boolean;
  className?: string;
}

export function ClarificationPromptCard({
  question,
  options,
  index,
  total,
  isPending,
  selectedAnswer,
  onSelectOption,
  disabled = false,
  className = "",
}: ClarificationPromptCardProps) {
  const [customInput, setCustomInput] = useState("");
  const [showCustomInput, setShowCustomInput] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submittingChoice, setSubmittingChoice] = useState<string | null>(null);

  const handleChoiceClick = async (choice: string) => {
    if (disabled || isSubmitting || !onSelectOption) return;
    try {
      setIsSubmitting(true);
      setSubmittingChoice(choice);
      await onSelectOption(choice);
    } finally {
      setIsSubmitting(false);
      setSubmittingChoice(null);
    }
  };

  const handleCustomSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = customInput.trim();
    if (!trimmed || disabled || isSubmitting || !onSelectOption) return;
    try {
      setIsSubmitting(true);
      setSubmittingChoice(trimmed);
      await onSelectOption(trimmed);
      setCustomInput("");
      setShowCustomInput(false);
    } finally {
      setIsSubmitting(false);
      setSubmittingChoice(null);
    }
  };

  const showOrdering = typeof index === "number" && typeof total === "number" && total > 1;

  if (!isPending) {
    // -------------------------------------------------------------------------
    // RESOLVED / COMPLETED STATE (Tier 1 subtle lift, calm emerald feedback)
    // -------------------------------------------------------------------------
    return (
      <div
        data-testid="clarification-card-resolved"
        className={`my-3 rounded-2xl border-t border-t-zinc-700/50 border-x border-x-zinc-800/60 border-b border-b-zinc-900 bg-gradient-to-b from-[#141418] to-[#0f0f12] p-3.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_2px_8px_rgba(0,0,0,0.3)] transition-all ${className}`}
      >
        <div className="flex items-start gap-3">
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-xl bg-emerald-500/15 text-emerald-400 border border-emerald-500/25">
            <CheckCircle2 className="h-4 w-4" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-[10px] font-mono uppercase tracking-wider text-emerald-400 font-semibold">
                Clarification Resolved
              </span>
              {showOrdering && (
                <span className="ml-auto rounded-full bg-zinc-800/80 border border-zinc-700/50 px-2 py-0.5 text-[10px] font-mono text-zinc-400">
                  Question {index} of {total}
                </span>
              )}
            </div>
            <p className="text-sm font-medium text-zinc-200 leading-snug">
              {question}
            </p>
            {selectedAnswer && (
              <div className="mt-2.5 flex items-center gap-2 rounded-xl bg-emerald-500/10 border border-emerald-500/20 px-3 py-1.5 text-xs font-mono text-emerald-300">
                <span className="text-zinc-400 font-sans">Selected response:</span>
                <span className="font-semibold text-emerald-200 break-words">{selectedAnswer}</span>
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // ---------------------------------------------------------------------------
  // ACTIVE / BLOCKING STATE (Tier 3 primary status accent, prominent painted-light)
  // ---------------------------------------------------------------------------
  return (
    <div
      data-testid="clarification-card-active"
      className={`my-3 rounded-2xl border-t border-t-amber-400/50 border-x border-x-amber-500/35 border-b border-b-amber-700/60 bg-gradient-to-b from-amber-500/15 via-[#16161c] to-[#101014] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_8px_32px_rgba(0,0,0,0.5)] backdrop-blur-xl animate-in fade-in slide-in-from-bottom-2 duration-300 ${className}`}
    >
      <div className="flex items-start gap-3">
        {/* Visual focal icon with glowing amber badge */}
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-amber-500/20 text-amber-300 border border-amber-500/40 shadow-[inset_0_1px_0_rgba(255,255,255,0.15)]">
          <HelpCircle className="h-4 w-4" />
        </div>

        <div className="flex-1 min-w-0">
          {/* Status banner with animated live beacon */}
          <div className="flex items-center gap-2 mb-1 flex-wrap">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-400" />
            </span>
            <span className="text-[10px] font-mono uppercase tracking-wider text-amber-400 font-semibold">
              Action Required · Agent Blocked
            </span>
            {showOrdering && (
              <span className="ml-auto rounded-full bg-amber-500/20 border border-amber-500/35 px-2 py-0.5 text-[10px] font-mono text-amber-300 font-semibold">
                Question {index} of {total}
              </span>
            )}
          </div>

          {/* Prominent Question Heading */}
          <h4 className="text-sm sm:text-[15px] font-semibold text-zinc-100 leading-snug tracking-tight">
            {question}
          </h4>

          <p className="text-xs text-zinc-400 mt-1">
            Agent execution is paused until you choose an option or provide input:
          </p>

          {/* Option Pills (Tactile 3D buttons) */}
          {options.length > 0 && (
            <div className="mt-3.5 flex flex-wrap gap-2">
              {options.map((option, optIdx) => {
                const isThisSubmitting = isSubmitting && submittingChoice === option;
                return (
                  <button
                    key={optIdx}
                    type="button"
                    onClick={() => handleChoiceClick(option)}
                    disabled={disabled || isSubmitting}
                    className="group relative flex items-center gap-1.5 rounded-xl border-t border-t-amber-300/60 border-x border-x-amber-500/40 border-b border-b-amber-800 bg-gradient-to-b from-amber-500/25 via-amber-500/15 to-amber-500/10 px-3.5 py-2 text-xs font-mono font-medium text-amber-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_6px_rgba(0,0,0,0.3)] hover:from-amber-500/35 hover:to-amber-500/20 hover:text-white hover:border-t-amber-300/80 active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.35)] transition-all duration-150 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400"
                  >
                    {isThisSubmitting ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-300 shrink-0" />
                    ) : (
                      <Check className="h-3.5 w-3.5 text-amber-400/90 group-hover:text-amber-300 shrink-0 transition-colors" />
                    )}
                    <span>{option}</span>
                  </button>
                );
              })}
            </div>
          )}

          {/* Custom response affordance */}
          <div className="mt-3 pt-2 border-t border-amber-500/20">
            {!showCustomInput ? (
              <button
                type="button"
                onClick={() => setShowCustomInput(true)}
                disabled={disabled || isSubmitting}
                className="text-[11px] font-mono text-zinc-400 hover:text-amber-300 transition-colors underline decoration-dotted underline-offset-4 cursor-pointer disabled:opacity-50"
              >
                + Type custom clarification response…
              </button>
            ) : (
              <form onSubmit={handleCustomSubmit} className="flex items-center gap-2 mt-1">
                <input
                  type="text"
                  value={customInput}
                  onChange={(e) => setCustomInput(e.target.value)}
                  placeholder="Type your instructions or answer…"
                  disabled={disabled || isSubmitting}
                  className="flex-1 rounded-xl border border-amber-500/30 bg-black/40 px-3 py-1.5 text-xs font-mono text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-amber-400 focus:ring-1 focus:ring-amber-400 disabled:opacity-50"
                  autoFocus
                />
                <button
                  type="submit"
                  disabled={!customInput.trim() || disabled || isSubmitting}
                  className="flex items-center gap-1 rounded-xl border-t border-t-amber-300/60 border-x border-x-amber-500/40 border-b border-b-amber-800 bg-amber-500/25 px-3 py-1.5 text-xs font-mono font-medium text-amber-100 hover:bg-amber-500/35 active:translate-y-[0.5px] transition-all disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {isSubmitting && submittingChoice === customInput.trim() ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Send className="h-3.5 w-3.5" />
                  )}
                  <span>Submit</span>
                </button>
                <button
                  type="button"
                  onClick={() => setShowCustomInput(false)}
                  disabled={isSubmitting}
                  className="px-2 py-1 text-xs text-zinc-500 hover:text-zinc-300 font-mono"
                >
                  Cancel
                </button>
              </form>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
