// Motion tokens for Motion's JS animations; tokens.css holds the same values for CSS
// transitions. Animations move only transform and opacity, so they stay on the compositor.

export const ease = {
  out: [0.16, 1, 0.3, 1] as const,
  inOut: [0.65, 0, 0.35, 1] as const,
};

export const duration = {
  fast: 0.12,
  base: 0.2,
  slow: 0.32,
};

export const spring = { type: "spring", stiffness: 520, damping: 42, mass: 1 } as const;
