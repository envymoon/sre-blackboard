import { Dialog } from "radix-ui";
import type { ComponentProps, ReactNode } from "react";
import { cn } from "@/lib/cn";

// A side drawer on Radix Dialog: focus trap, Escape, scroll lock and aria wiring come from
// Radix. It slides with transform only (the sheet-in / sheet-out keyframes in index.css).

export const Sheet = Dialog.Root;
export const SheetTitle = Dialog.Title;
export const SheetDescription = Dialog.Description;
export const SheetClose = Dialog.Close;

export function SheetContent({
  className,
  children,
  ...props
}: ComponentProps<typeof Dialog.Content> & { children: ReactNode }) {
  return (
    <Dialog.Portal>
      <Dialog.Overlay className="sheet-overlay fixed inset-0 z-40 bg-overlay" />
      <Dialog.Content
        className={cn(
          "sheet fixed inset-y-0 right-0 z-50 flex w-full max-w-[440px] flex-col border-l border-line bg-sunken shadow-drawer outline-none",
          className,
        )}
        {...props}
      >
        {children}
      </Dialog.Content>
    </Dialog.Portal>
  );
}
