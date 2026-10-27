import "@fontsource-variable/inter/wght.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "./styles/index.css";

import { LazyMotion, MotionConfig } from "motion/react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createHashRouter, Navigate, RouterProvider } from "react-router";

const pages = import.meta.env.VITE_SITE === "pages";

// Every page is its own chunk; the first screen loads only the one it shows.
const router = createHashRouter([
  {
    path: "/",
    HydrateFallback: () => null,
    children: [
      pages
        ? { index: true, lazy: async () => ({ Component: (await import("./pages/Landing")).default }) }
        : { index: true, element: <Navigate to="/incidents" replace /> },
      { path: "login", lazy: async () => ({ Component: (await import("./app/Login")).default }) },
      {
        path: "incidents",
        lazy: async () => ({ Component: (await import("./app/LiveApp")).default }),
        children: [
          { index: true, lazy: async () => ({ Component: (await import("./app/LiveApp")).ListPage }) },
          {
            path: ":id",
            lazy: async () => ({ Component: (await import("./features/incident/IncidentPage")).default }),
          },
        ],
      },
      { path: "replay/:case?", lazy: async () => ({ Component: (await import("./app/ReplayApp")).default }) },
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
]);

// Motion's animation features load after the first paint; `m` components render plain until then.
const features = () => import("./styles/motion-features").then((r) => r.default);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MotionConfig reducedMotion="user">
      <LazyMotion features={features} strict>
        <RouterProvider router={router} />
      </LazyMotion>
    </MotionConfig>
  </StrictMode>,
);
