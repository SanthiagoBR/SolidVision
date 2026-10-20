import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter } from "react-router";
import { RouterProvider } from "react-router/dom";

import { Home } from "./home/Home";
import { NotFound } from "./shell/NotFound";
import { Shell } from "./shell/Shell";

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
