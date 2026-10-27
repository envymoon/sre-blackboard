// "Now" for relative times and countdowns: the wall clock in the console, the replay clock
// in a replay. Components that show a countdown subscribe; nothing else re-renders on a tick.

import { createContext, useContext, useSyncExternalStore } from "react";

export interface Clock {
  subscribe(fn: () => void): () => void;
  now(): number;
}

let wall = Date.now();
const wallClock: Clock = {
  subscribe(fn) {
    const t = setInterval(() => {
      wall = Date.now();
      fn();
    }, 1000);
    return () => clearInterval(t);
  },
  now: () => wall,
};

export const ClockContext = createContext<Clock>(wallClock);

export function useNow(): number {
  const clock = useContext(ClockContext);
  return useSyncExternalStore(clock.subscribe, clock.now);
}
