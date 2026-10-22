import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import { createBrowserRouter } from "react-router";
import { RouterProvider } from "react-router/dom";

import { FolderScreen } from "./devices/FolderScreen";
import { Home } from "./home/Home";
import { SearchScreen } from "./search/SearchScreen";
import { NotFound } from "./shell/NotFound";
import { Shell } from "./shell/Shell";

// The map, the Leaflet and the outline of the states are a part of the build
// of their own, loaded when the screen opens: the rest of the interface does
// not grow (RFC-040 section 5.1).
const MapScreen = lazy(() => import("./map/MapScreen"));

const queryClient = new QueryClient({
  defaultOptions: {
    // A local API: an error is worth one more try, not three.
    queries: { retry: 1 },
  },
});

// The window coming back to the front refetches (RFC-037 section 7). TanStack
// Query listens to `visibilitychange` alone, which a window behind another,
// and not minimized, never fires.
focusManager.setEventListener((handleFocus) => {
  const changed = () => handleFocus();
  window.addEventListener("visibilitychange", changed);
  window.addEventListener("focus", changed);
  return () => {
    window.removeEventListener("visibilitychange", changed);
    window.removeEventListener("focus", changed);
  };
});

const router = createBrowserRouter([
  {
    path: "/",
    element: <Shell />,
    children: [
      { index: true, element: <Home /> },
      { path: "busca", element: <SearchScreen /> },
      // The framing, the search and the filters in the URL (RFC-040 5.1).
      {
        path: "mapa",
        element: (
          <Suspense fallback={null}>
            <MapScreen />
          </Suspense>
        ),
      },
      // One device's folders; `?pasta=` is the level (RFC-039 section 5.1).
      { path: "dispositivos/:id", element: <FolderScreen /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
