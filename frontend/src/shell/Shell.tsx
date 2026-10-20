/**
 * The shell: the header, the sidebar and the content (RFC-037 section 7).
 *
 * The sidebar is, at this stage, the list of devices with their label and
 * whether the disk is plugged in; the cards of the Figma, with the counts and
 * the percentage beside the scan date (RFC-031 section 12), are RFC-039's.
 */

import { Outlet } from "react-router";

import type { Device } from "../api/client";
import { useDevices } from "../api/queries";
import styles from "./Shell.module.css";

export function Shell() {
  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <span className={styles.wordmark}>SolidVision</span>
      </header>
      <aside className={styles.sidebar} aria-labelledby="sidebar-title">
        <h2 id="sidebar-title" className={styles.sidebarTitle}>
          Dispositivos
        </h2>
        <DeviceList />
      </aside>
      <main className={styles.content}>
        <Outlet />
      </main>
    </div>
  );
}

function DeviceList() {
  const devices = useDevices();
  if (devices.isPending) {
    return <p className={styles.sidebarNote}>Lendo os dispositivos…</p>;
  }
  if (devices.isError) {
    return <p className={styles.sidebarNote}>Não foi possível ler os dispositivos.</p>;
  }
  if (devices.data.length === 0) {
    return <p className={styles.sidebarNote}>Nenhum dispositivo registrado</p>;
  }
  return (
    <ul className={styles.devices}>
      {devices.data.map((device) => (
        <DeviceRow key={device.id} device={device} />
      ))}
    </ul>
  );
}

function DeviceRow({ device }: { device: Device }) {
  const state = device.connected ? "conectado" : "desconectado";
  return (
    <li className={styles.device}>
      <span
        className={device.connected ? styles.dotConnected : styles.dotDisconnected}
        aria-hidden="true"
      />
      <span className={styles.deviceLabel}>{device.label}</span>
      <span className={styles.deviceState}>{state}</span>
    </li>
  );
}
