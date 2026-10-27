import type { ComponentProps } from "react";
import { cn } from "@/lib/cn";

/** A placeholder with the size of what will replace it, so nothing moves when data arrives. */
export function Skeleton({ className, ...props }: ComponentProps<"div">) {
  return <div aria-hidden="true" className={cn("skeleton rounded-md bg-raised", className)} {...props} />;
}
