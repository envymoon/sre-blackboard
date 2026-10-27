import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// tailwind-merge has to know the custom type scale, or it takes text-sm and text-muted for
// the same kind of class and drops one of them.
const merge = extendTailwindMerge({
  extend: { classGroups: { "font-size": [{ text: ["2xs", "xs", "sm", "base", "md", "lg", "xl", "2xl"] }] } },
});

export function cn(...inputs: ClassValue[]): string {
  return merge(clsx(inputs));
}
