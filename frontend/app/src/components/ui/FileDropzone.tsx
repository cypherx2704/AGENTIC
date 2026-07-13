'use client';

import { useRef, useState, type DragEvent } from 'react';
import { cn } from '@/lib/utils';

/**
 * A restrained click-or-drag file picker. Controlled: the parent owns the selected File and
 * decides what to do with it (RAG presigned upload, etc.). Styles through tokens only — no
 * decorative flourish, just a dashed drop target that reacts to drag-over and disabled state.
 */
export function FileDropzone({
  onFile,
  accept,
  disabled = false,
  hint = 'Drag a file here, or click to browse.',
  selected,
  className,
}: {
  onFile: (file: File) => void;
  /** Native accept string, e.g. '.pdf,.md,.txt,text/markdown'. */
  accept?: string;
  disabled?: boolean;
  hint?: string;
  /** The currently-selected file, if any (shown as a chip so the row reads as "chosen"). */
  selected?: File | null;
  className?: string;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  function pick(file: File | undefined | null) {
    if (file && !disabled) onFile(file);
  }

  function onDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragging(false);
    if (disabled) return;
    pick(e.dataTransfer.files?.[0]);
  }

  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled}
      onClick={() => !disabled && inputRef.current?.click()}
      onKeyDown={(e) => {
        if ((e.key === 'Enter' || e.key === ' ') && !disabled) {
          e.preventDefault();
          inputRef.current?.click();
        }
      }}
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      className={cn(
        'flex flex-col items-center justify-center gap-1.5 rounded-md border border-dashed px-4 py-6 text-center transition-colors',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/50',
        disabled
          ? 'cursor-not-allowed border-border bg-surface-2 opacity-60'
          : dragging
            ? 'cursor-pointer border-brand bg-brand/5'
            : 'cursor-pointer border-border bg-surface-2 hover:border-brand/50',
        className,
      )}
    >
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        disabled={disabled}
        className="hidden"
        onChange={(e) => {
          pick(e.target.files?.[0]);
          e.target.value = ''; // allow re-selecting the same file
        }}
      />
      {selected ? (
        <span className="max-w-full truncate font-mono text-xs text-fg">{selected.name}</span>
      ) : (
        <span className="text-sm font-medium text-fg">Choose A File</span>
      )}
      <span className="text-xs text-muted">{hint}</span>
    </div>
  );
}
