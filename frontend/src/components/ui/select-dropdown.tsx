"use client";

import * as React from "react";
import { useEffect, useRef, useState, useMemo } from "react";
import { ChevronDown, Check, Search, X } from "lucide-react";
import { cn } from "@/lib/utils";

export interface SelectOption {
  value: string;
  label: string;
  badge?: React.ReactNode;
  icon?: React.ReactNode;
  description?: string;
}

export interface SelectDropdownProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  className?: string;
  buttonClassName?: string;
  menuClassName?: string;
  disabled?: boolean;
  searchable?: boolean;
  id?: string;
  "aria-label"?: string;
}

export function SelectDropdown({
  value,
  onChange,
  options,
  placeholder = "Select...",
  className,
  buttonClassName,
  menuClassName,
  disabled = false,
  searchable,
  id,
  "aria-label": ariaLabel,
}: SelectDropdownProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [highlightedIndex, setHighlightedIndex] = useState<number>(-1);
  const containerRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const selectedOption = options.find((opt) => opt.value === value);

  // Search only enabled if > 8 items or explicitly enabled
  const isSearchEnabled = searchable ?? options.length > 8;

  const filteredOptions = useMemo(() => {
    if (!searchQuery.trim()) return options;
    const q = searchQuery.toLowerCase().trim();
    return options.filter(
      (opt) =>
        opt.label.toLowerCase().includes(q) ||
        (opt.description && opt.description.toLowerCase().includes(q)) ||
        opt.value.toLowerCase().includes(q)
    );
  }, [options, searchQuery]);

  useEffect(() => {
    if (isOpen) {
      setSearchQuery("");
      const currentIndex = filteredOptions.findIndex((opt) => opt.value === value);
      setHighlightedIndex(currentIndex >= 0 ? currentIndex : 0);
      if (isSearchEnabled) {
        setTimeout(() => searchInputRef.current?.focus(), 40);
      }
    }
  }, [isOpen]);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent | TouchEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(event.target as Node)
      ) {
        setIsOpen(false);
      }
    };

    if (isOpen) {
      document.addEventListener("mousedown", handleClickOutside);
      document.addEventListener("touchstart", handleClickOutside);
    }

    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("touchstart", handleClickOutside);
    };
  }, [isOpen]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return;

    if (e.key === "Escape") {
      setIsOpen(false);
      return;
    }

    if (!isOpen) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        setIsOpen(true);
      }
      return;
    }

    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlightedIndex((prev) =>
        prev < filteredOptions.length - 1 ? prev + 1 : 0
      );
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlightedIndex((prev) =>
        prev > 0 ? prev - 1 : filteredOptions.length - 1
      );
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (highlightedIndex >= 0 && filteredOptions[highlightedIndex]) {
        handleSelect(filteredOptions[highlightedIndex].value);
      }
    } else if (e.key === "Tab") {
      setIsOpen(false);
    }
  };

  const handleSelect = (val: string) => {
    onChange(val);
    setIsOpen(false);
  };

  return (
    <div
      ref={containerRef}
      className={cn("relative inline-block text-left", isOpen ? "z-40" : "z-auto", className)}
      onKeyDown={handleKeyDown}
    >
      <button
        type="button"
        id={id}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        disabled={disabled}
        onClick={() => !disabled && setIsOpen(!isOpen)}
        className={cn(
          "group h-8.5 flex items-center justify-between gap-2 rounded-[6px] border border-zinc-800/80 bg-zinc-900/60 px-3 text-[12.5px] text-zinc-300 font-normal transition-all duration-150 select-none cursor-pointer",
          "hover:border-zinc-700/90 hover:bg-zinc-900/90 hover:text-zinc-100",
          "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-zinc-600 focus-visible:border-zinc-600",
          isOpen && "border-zinc-700 bg-zinc-900 text-zinc-100 ring-1 ring-zinc-700/50 shadow-sm",
          disabled && "opacity-50 cursor-not-allowed pointer-events-none",
          buttonClassName
        )}
      >
        <div className="flex items-center gap-2 truncate min-w-0">
          {selectedOption?.badge}
          {selectedOption?.icon}
          <span className="truncate">
            {selectedOption ? selectedOption.label : placeholder}
          </span>
        </div>
        <ChevronDown
          className={cn(
            "h-3.5 w-3.5 shrink-0 text-zinc-500 transition-transform duration-150 ease-out",
            "group-hover:text-zinc-400",
            isOpen && "rotate-180 text-zinc-300"
          )}
        />
      </button>

      {isOpen && (
        <div
          role="listbox"
          className={cn(
            "absolute left-0 top-full mt-1.5 z-50 min-w-[200px] w-max max-w-sm rounded-[8px] border border-zinc-800 bg-[#121215] p-1 shadow-[0_12px_32px_rgba(0,0,0,0.7),0_0_0_1px_rgba(255,255,255,0.03)] focus:outline-none",
            menuClassName
          )}
        >
          {/* Search bar if enabled */}
          {isSearchEnabled && (
            <div className="relative mb-1 px-1 pt-0.5">
              <div className="relative flex items-center">
                <Search className="absolute left-2.5 h-3.5 w-3.5 text-zinc-500 pointer-events-none" />
                <input
                  ref={searchInputRef}
                  type="text"
                  placeholder="Filter..."
                  value={searchQuery}
                  onChange={(e) => {
                    setSearchQuery(e.target.value);
                    setHighlightedIndex(0);
                  }}
                  className="w-full h-7.5 pl-8 pr-7 bg-zinc-900/80 border border-zinc-800/80 rounded-[5px] text-[12px] text-zinc-200 placeholder:text-zinc-500 focus:outline-none focus:border-zinc-700 focus:ring-1 focus:ring-zinc-700"
                />
                {searchQuery && (
                  <button
                    type="button"
                    onClick={() => setSearchQuery("")}
                    className="absolute right-2 text-zinc-500 hover:text-zinc-300 p-0.5"
                  >
                    <X className="h-3 w-3" />
                  </button>
                )}
              </div>
            </div>
          )}

          {/* Options list */}
          <div
            ref={listRef}
            className="max-h-60 overflow-y-auto space-y-0.5 pr-0.5"
          >
            {filteredOptions.length === 0 ? (
              <div className="py-3 text-center text-[12px] text-zinc-500">
                No matching options
              </div>
            ) : (
              filteredOptions.map((option, index) => {
                const isSelected = option.value === value;
                const isHighlighted = index === highlightedIndex;

                return (
                  <button
                    key={option.value}
                    type="button"
                    role="option"
                    aria-selected={isSelected}
                    onClick={() => handleSelect(option.value)}
                    onMouseEnter={() => setHighlightedIndex(index)}
                    className={cn(
                      "w-full flex items-center justify-between gap-2.5 px-2.5 py-1.5 text-[12.5px] rounded-[5px] text-left transition-colors cursor-pointer select-none",
                      isSelected
                        ? "bg-zinc-800/70 text-zinc-100 font-medium"
                        : isHighlighted
                        ? "bg-zinc-800/40 text-zinc-200"
                        : "text-zinc-400 hover:bg-zinc-800/30 hover:text-zinc-200"
                    )}
                  >
                    <div className="flex items-center gap-2 truncate min-w-0">
                      {option.badge && (
                        <span className="shrink-0 flex items-center justify-center">
                          {option.badge}
                        </span>
                      )}
                      {option.icon && (
                        <span className="shrink-0 text-zinc-400">
                          {option.icon}
                        </span>
                      )}
                      <div className="flex flex-col truncate">
                        <span className="truncate">
                          {option.label}
                        </span>
                        {option.description && (
                          <span className="text-[11px] text-zinc-500 truncate mt-0.5 font-normal">
                            {option.description}
                          </span>
                        )}
                      </div>
                    </div>
                    {isSelected && (
                      <Check className="h-3.5 w-3.5 text-amber-400 shrink-0 ml-1.5" />
                    )}
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}
