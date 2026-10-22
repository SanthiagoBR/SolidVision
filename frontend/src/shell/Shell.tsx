/**
 * The shell: the header, the sidebar and the content (RFC-037 section 7).
 *
 * Since RFC-039 the sidebar holds the cards of the Figma -- the label, the
 * connection, the photos, the percentage beside the date of the scan, and the
 * job -- and "+ Adicionar dispositivo" (`devices/DeviceCards.tsx`). The shell
 * also listens to the window's volume messages, which ask `/devices` at once
 * (section 5.9), keeps the job runner's state for every screen, and shows its
 * banner above the content when it is unavailable (section 5.8).
 *
 * Since RFC-040 (section 5.1) the header has "Busca" and "Mapa" beside the
 * name, the open screen's in relief. A click on "Mapa" starts the map's mark
 * (section 5.6), before its part of the build has loaded.
 */

import { useEffect } from "react";
import { Link, Outlet, useLocation } from "react-router";

import { useVolumeMessages } from "../api/queries";
import { DeviceCards } from "../devices/DeviceCards";
import { IndexingBanner, IndexingProvider } from "../devices/indexing";
import { startMapGesture } from "../map/timing";
import { MAP_PATH } from "../map/url";
import styles from "./Shell.module.css";

export function Shell() {
  usePreventFileNavigation();
  useVolumeMessages();
  const { pathname } = useLocation();
  const onSearch = pathname === "/" || pathname === "/busca";
  const onMap = pathname === MAP_PATH;
  return (
    <IndexingProvider>
      <div className={styles.shell}>
        <header className={styles.header}>
          <span className={styles.wordmark}>SolidVision</span>
          <nav className={styles.nav} aria-label="Telas">
            <Link
              className={onSearch ? styles.navCurrent : styles.navLink}
              aria-current={onSearch ? "page" : undefined}
              to="/"
            >
              Busca
            </Link>
            <Link
              className={onMap ? styles.navCurrent : styles.navLink}
              aria-current={onMap ? "page" : undefined}
              to={MAP_PATH}
              onClick={startMapGesture}
            >
              Mapa
            </Link>
          </nav>
        </header>
        <aside className={styles.sidebar} aria-labelledby="sidebar-title">
          <h2 id="sidebar-title" className={styles.sidebarTitle}>
            Dispositivos
          </h2>
          <DeviceCards />
        </aside>
        <main className={styles.content}>
          <IndexingBanner />
          <Outlet />
        </main>
      </div>
    </IndexingProvider>
  );
}

/**
 * A file dropped anywhere but the search bar does nothing (RFC-038 section
 * 5.5). Chromium's default is to open it in the window itself; the
 * `will-navigate` of RFC-037 refuses a navigation off the origin, and
 * cancelling here keeps it from being tried at all.
 */
function usePreventFileNavigation(): void {
  useEffect(() => {
    const cancel = (event: DragEvent) => event.preventDefault();
    document.addEventListener("dragover", cancel);
    document.addEventListener("drop", cancel);
    return () => {
      document.removeEventListener("dragover", cancel);
      document.removeEventListener("drop", cancel);
    };
  }, []);
}
