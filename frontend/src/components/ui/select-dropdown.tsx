"use client";

import * as React from "react";
import { useEffect, useRef, useState } from "react";
import { ChevronDown, Check } from "lucide-react";
import { cn } from "@/lib/utils";

export interface SelectOption {
  value: string;
  label: string;
  badge?: React.ReactNode;
  icon?: React.ReactNode;
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
  id,
  "aria-label": ariaLabel,
}: SelectDropdownProps) {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const selectedOption = options.find((opt) => opt.value === value);

  // Close when clicking outside
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

  // Handle keyboard events (ESC, arrows)
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return;

    if (e.key === "Escape") {
      setIsOpen(false);
      return;
    }

    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!isOpen) {
        setIsOpen(true);
        return;
      }

      const currentIndex = options.findIndex((opt) => opt.value === value);
      let nextIndex = currentIndex;

      if (e.key === "ArrowDown") {
        nextIndex = currentIndex < options.length - 1 ? currentIndex + 1 : 0;
      } else {
        nextIndex = currentIndex > 0 ? currentIndex - 1 : options.length - 1;
      }

      if (options[nextIndex]) {
        onChange(options[nextIndex].value);
      }
    }

    if (e.key === "Enter" || e.key === " ") {
      if (!isOpen) {
        e.preventDefault();
        setIsOpen(true);
      }
    }
  };

  const handleSelect = (val: string) => {
    onChange(val);
    setIsOpen(false);
  };

  return (
    <div
      ref={containerRef}
      className={cn("relative inline-block text-left", className)}
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
          "h-10 flex items-center justify-between gap-2.5 rounded-[7px] border border-zinc-800 bg-[#0c0c0e] px-3.5 text-[13px] text-zinc-200 transition-all select-none",
          "hover:border-zinc-700 hover:bg-[#101013] focus:outline-none",
          isOpen && "border-amber-400/80 ring-1 ring-amber-400/30",
          disabled && "opacity-50 cursor-not-allowed",
          buttonClassName
        )}
      >
        <span className="flex items-center gap-2 truncate">
          {selectedOption?.badge}
          {selectedOption?.icon}
          <span className="truncate">
            {selectedOption ? selectedOption.label : placeholder}
          </span>
        </span>
        <ChevronDown
          className={cn(
            "h-4 w-4 shrink-0 text-zinc-400 transition-transform duration-200",
            isOpen && "rotate-180 text-amber-400"
          )}
        />
      </button>

      {isOpen && (
        <div
          role="listbox"
          className={cn(
            "absolute left-0 top-full mt-1.5 z-50 min-w-[200px] w-max max-w-xs rounded-[8px] border border-zinc-800 bg-[#121215] p-1 shadow-2xl shadow-black/80 backdrop-blur-md max-h-60 overflow-y-auto focus:outline-none",
            menuClassName
          )}
        >
          {options.map((option) => {
            const isSelected = option.value === value;
            return (
              <button
                key={option.value}
                type="button"
                role="option"
                aria-selected={isSelected}
                onClick={() => handleSelect(option.value)}
                className={cn(
                  "w-full flex items-center justify-between gap-2 px-2.5 py-2 text-[13px] rounded-[5px] text-left transition-colors cursor-pointer select-none",
                  isSelected
                    ? "bg-amber-400/10 text-amber-400 font-medium"
                    : "text-zinc-300 hover:bg-zinc-800/60 hover:text-zinc-100"
                )}
              >
                <div className="flex items-center gap-2 truncate">
                  {option.badge}
                  {option.icon}
                  <span className="truncate">{option.label}</span>
                </div>
                {isSelected && (
                  <Check className="h-3.5 w-3.5 text-amber-400 shrink-0" />
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
