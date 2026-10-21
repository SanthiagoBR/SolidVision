/**
 * The shell: the header, the sidebar and the content (RFC-037 section 7).
 *
 * Since RFC-039 the sidebar holds the cards of the Figma -- the label, the
 * connection, the photos, the percentage beside the date of the scan, and the
 * job -- and "+ Adicionar dispositivo" (`devices/DeviceCards.tsx`). The shell
 * also listens to the window's volume messages, which ask `/devices` at once
 * (section 5.9), keeps the job runner's state for every screen, and shows its
 * banner above the content when it is unavailable (section 5.8).
 */

import { useEffect } from "react";
import { Outlet } from "react-router";

import { useVolumeMessages } from "../api/queries";
import { DeviceCards } from "../devices/DeviceCards";
import { IndexingBanner, IndexingProvider } from "../devices/indexing";
import styles from "./Shell.module.css";

export function Shell() {
  usePreventFileNavigation();
  useVolumeMessages();
  return (
    <IndexingProvider>
      <div className={styles.shell}>
        <header className={styles.header}>
          <span className={styles.wordmark}>SolidVision</span>
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
