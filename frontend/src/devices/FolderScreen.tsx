/**
 * `/dispositivos/:id?pasta=…`: one device's folders (RFC-039 section 5.6),
 * node 14:51 of the Figma with the corrections of RFC-031 section 12.
 *
 *     the header      the label, with "Renomear"; where the disk is, or
 *                     "desconectado"; the card's line; "Indexar o disco
 *                     inteiro"
 *     the panel       the disk's most recent job (section 5.7)
 *     the trail       "HD2 › 2018 › junho", each part a link to its level
 *     the list        one level, in Explorer's order, windowed, with a
 *                     filter above 50 folders
 *     the action bar  "3 pastas selecionadas", "Limpar", "Indexar as pastas"
 *
 *     when                                     the screen shows
 *     /devices has not the id, or a 404        "Esse dispositivo não está
 *                                              registrado.", and the way home
 *     the disk away                            the header, the panel, and
 *                                              "Conecte o HD2 para ver as
 *                                              pastas.", without asking for
 *                                              the listing; it is asked by
 *                                              itself when the disk is back
 *     the listing running for 300 ms           "Lendo as pastas do HD2…"
 *     a 400                                    the folder is no longer there,
 *                                              and the way to its parent
 *     no subfolder                             "Nenhuma subpasta em junho."
 *
 * **The disk away comes from `/devices`**, not from a 409 of the listing: the
 * screen changes when the card does, and does not wait for a request that
 * would fail. The 409 happens only in a race, and leads to the same screen.
 *
 * The selection lives here, per device, while one walks the disk; a reload
 * loses it, and that is declared (section 9).
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import { ApiError, type Device } from "../api/client";
import { keys } from "../api/keys";
import { useDevices, useFolders } from "../api/queries";
import {
  conflictMessage,
  createJob,
  renameDevice,
  storeDevice,
  storeNewJob,
} from "./api";
import { cardPhotosText } from "./card";
import { FolderList } from "./FolderList";
import {
  devicePath,
  FILTER_FROM,
  filterFolders,
  lastPart,
  parentOf,
  sortFolders,
  trail,
} from "./folders";
import styles from "./FolderScreen.module.css";
import { useIndexing } from "./indexing";
import { JobPanel } from "./JobPanel";
import { labelProblem } from "./labels";
import {
  foldersErrorText,
  indexBlockedText,
  jobErrorText,
  NOT_REGISTERED,
  renameErrorText,
} from "./messages";
import { deselect, EMPTY, scopesOf, select, type Selection } from "./selection";
import { scanShare } from "./share";
import { startFoldersGesture } from "./timing";
import { dayText, foldersCountText, formatCount, instant, mountText } from "./words";

const SLOW_MS = 300;

export function FolderScreen() {
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const path = params.get("pasta") ?? "";
  const devices = useDevices();

  if (devices.isPending) {
    return <p className={styles.note}>Lendo os dispositivos…</p>;
  }
  if (devices.data === undefined) {
    return <p className={styles.note}>Não foi possível ler os dispositivos.</p>;
  }
  const device = devices.data.find((known) => known.id === id);
  if (device === undefined) {
    return <NotRegistered />;
  }
  const others = devices.data.filter((known) => known.id !== device.id);
  return <DeviceFolders key={device.id} device={device} others={others} path={path} />;
}

function NotRegistered() {
  return (
    <div className={styles.gone}>
      <p className={styles.goneText}>Esse dispositivo não está registrado.</p>
      <Link className={styles.link} to="/">
        Voltar ao início
      </Link>
    </div>
  );
}

function DeviceFolders({
  device,
  others,
  path,
}: {
  device: Device;
  others: readonly Device[];
  path: string;
}) {
  const client = useQueryClient();
  const indexing = useIndexing();
  const [selection, setSelection] = useState<Selection>(EMPTY);
  const [confirming, setConfirming] = useState(false);
  const [sending, setSending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const blocked = indexBlockedText(device, indexing.status);

  const index = async (scopes: readonly string[]) => {
    setSending(true);
    setMessage(null);
    try {
      storeNewJob(client, await createJob(device.id, scopes));
      setSelection(EMPTY);
    } catch (error) {
      const status = error instanceof ApiError ? error.status : -1;
      if (status === 409) {
        // The disk left, or got a job: /devices says which (section 2.5).
        setMessage(await conflictMessage(client, device.id));
      } else {
        setMessage(jobErrorText(status, device.label));
        if (status === 400) {
          void client.invalidateQueries({ queryKey: keys.folders(device.id) });
        }
        if (status === 404) {
          void client.invalidateQueries({ queryKey: keys.devices });
        }
      }
    } finally {
      setSending(false);
    }
  };

  return (
    <div className={styles.screen}>
      <Header
        device={device}
        others={others}
        blocked={blocked}
        onWholeDisk={() => setConfirming(true)}
      />
      <JobPanel device={device} />
      <Trail device={device} path={path} />
      <Level
        // A level of its own: the filter and the wait start over at each one.
        key={path}
        device={device}
        path={path}
        selection={selection}
        onToggle={(folder) =>
          setSelection((current) =>
            current.has(folder) ? deselect(current, folder) : select(current, folder),
          )
        }
      />
      <ActionBar
        selection={selection}
        blocked={blocked}
        sending={sending}
        message={message}
        onClear={() => setSelection(EMPTY)}
        onIndex={() => void index(scopesOf(selection))}
      />
      {confirming && (
        <ConfirmWholeDisk
          device={device}
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            setConfirming(false);
            void index([]);
          }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The header

function Header({
  device,
  others,
  blocked,
  onWholeDisk,
}: {
  device: Device;
  others: readonly Device[];
  blocked: string | null;
  onWholeDisk: () => void;
}) {
  const share = scanShare(device);
  return (
    <header className={styles.header}>
      <Rename device={device} others={others} />
      <p className={styles.meta}>
        <span className={styles.metaPart}>
          <span
            className={device.connected ? styles.dotConnected : styles.dotDisconnected}
            aria-hidden="true"
          />
          {device.connected
            ? `conectado em ${mountText(device.mount_point)}`
            : "desconectado"}
        </span>
        <span className={styles.metaPart}>{cardPhotosText(device)}</span>
        {share.kind !== "none" && <span className={styles.metaPart}>{share.text}</span>}
      </p>
      <div className={styles.headerActions}>
        <button
          type="button"
          className={styles.primary}
          disabled={blocked !== null}
          onClick={onWholeDisk}
        >
          Indexar o disco inteiro
        </button>
        {blocked !== null && <span className={styles.reason}>{blocked}</span>}
      </div>
    </header>
  );
}

/** The label, and "Renomear", which turns it into a field: Enter saves by
 * `PATCH /devices/{id}`, Esc gives up (section 5.5). It works with the disk
 * away (RFC-031 section 7), and the answer, a row of `/devices`, goes straight
 * into the cache. */
function Rename({ device, others }: { device: Device; others: readonly Device[] }) {
  const client = useQueryClient();
  const fieldId = useId();
  const [draft, setDraft] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  if (draft === null) {
    return (
      <div className={styles.titleRow}>
        <h1 className={styles.title}>{device.label}</h1>
        <button
          type="button"
          className={styles.textButton}
          onClick={() => {
            setMessage(null);
            setDraft(device.label);
          }}
        >
          Renomear
        </button>
        {message !== null && (
          <p className={styles.message} role="alert">
            {message}
          </p>
        )}
      </div>
    );
  }

  const problem = labelProblem(
    draft,
    others.map((other) => other.label),
  );

  const save = async () => {
    if (problem !== null || saving) {
      return;
    }
    if (draft.trim() === device.label) {
      setDraft(null);
      return;
    }
    setSaving(true);
    try {
      storeDevice(client, await renameDevice(device.id, draft));
      setDraft(null);
    } catch (error) {
      const status = error instanceof ApiError ? error.status : -1;
      if (status === 422) {
        // A defect: the interface sends no blank label.
        console.error("O PATCH /devices respondeu 422");
      }
      if (status === 404) {
        void client.invalidateQueries({ queryKey: keys.devices });
      }
      setMessage(renameErrorText(status));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form
      className={styles.titleRow}
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <label className={styles.visuallyHidden} htmlFor={fieldId}>
        Novo nome do {device.label}
      </label>
      <input
        id={fieldId}
        className={styles.titleField}
        value={draft}
        maxLength={80}
        // The field exists because the button was just pressed.
        autoFocus
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setDraft(null);
            setMessage(null);
          }
        }}
        aria-invalid={problem !== null}
      />
      <button
        type="submit"
        className={styles.primary}
        disabled={problem !== null || saving}
      >
        Salvar
      </button>
      <button
        type="button"
        className={styles.textButton}
        onClick={() => setDraft(null)}
      >
        Cancelar
      </button>
      {problem?.kind === "repeated" && (
        <span className={styles.reason}>{problem.text}</span>
      )}
      {message !== null && (
        <p className={styles.message} role="alert">
          {message}
        </p>
      )}
    </form>
  );
}

// ---------------------------------------------------------------------------
// The trail and the level

function Trail({ device, path }: { device: Device; path: string }) {
  const parts = trail(device.label, path);
  return (
    <nav className={styles.trail} aria-label="Onde você está no disco">
      {parts.map((part, index) =>
        index === parts.length - 1 ? (
          <span key={part.path} className={styles.trailHere} aria-current="page">
            {part.name}
          </span>
        ) : (
          <span key={part.path} className={styles.trailPart}>
            <Link
              to={devicePath(device.id, part.path)}
              className={styles.trailLink}
              onClick={() => startFoldersGesture(device.id, part.path)}
            >
              {part.name}
            </Link>
            <span className={styles.trailSeparator} aria-hidden="true">
              ›
            </span>
          </span>
        ),
      )}
    </nav>
  );
}

function Level({
  device,
  path,
  selection,
  onToggle,
}: {
  device: Device;
  path: string;
  selection: Selection;
  onToggle: (path: string) => void;
}) {
  const client = useQueryClient();
  const folders = useFolders(device.id, path, device.connected);
  const [filter, setFilter] = useState("");
  const filterId = useId();
  const slow = useSlow(folders.isFetching && folders.data === undefined);
  const error = folders.error;
  const status = error instanceof ApiError ? error.status : error ? -1 : null;

  useEffect(() => {
    if (status === 409 || status === 404) {
      // A race: the disk left, or the device did, after /devices was read.
      void client.invalidateQueries({ queryKey: keys.devices });
    }
  }, [status, client]);

  const sorted = useMemo(
    () => (folders.data !== undefined ? sortFolders(folders.data.folders) : []),
    [folders.data],
  );
  const shown = useMemo(() => filterFolders(sorted, filter), [sorted, filter]);
  const atRoot = path === "";
  const name = lastPart(path);

  if (!device.connected || status === 409) {
    return <p className={styles.state}>Conecte o {device.label} para ver as pastas.</p>;
  }
  if (status !== null && folders.data === undefined) {
    if (status === 404) {
      return <p className={styles.state}>{NOT_REGISTERED}</p>;
    }
    const parent = parentOf(path);
    return (
      <div className={styles.stateBlock}>
        <p className={styles.state} role="alert">
          {foldersErrorText(status === -1 ? 500 : status, device.label, name)}
        </p>
        {status === 400 && !atRoot && (
          <Link
            className={styles.link}
            to={devicePath(device.id, parent)}
            onClick={() => startFoldersGesture(device.id, parent)}
          >
            Voltar para {parent === "" ? device.label : lastPart(parent)}
          </Link>
        )}
      </div>
    );
  }
  if (folders.data === undefined) {
    return (
      <p className={styles.state} aria-busy="true">
        {slow ? `Lendo as pastas do ${device.label}…` : " "}
      </p>
    );
  }

  const level = folders.data;
  const rootHint =
    atRoot && (level.folders.length === 0 || device.indexed_images === 0)
      ? "As fotos que estiverem direto na raiz entram com Indexar o disco inteiro."
      : null;

  if (level.folders.length === 0) {
    return (
      <div className={styles.stateBlock}>
        <p className={styles.state}>
          {atRoot
            ? `Nenhuma pasta na raiz do ${device.label}.`
            : `Nenhuma subpasta em ${name}.`}
        </p>
        {rootHint !== null && <p className={styles.hint}>{rootHint}</p>}
      </div>
    );
  }

  return (
    <>
      {rootHint !== null && <p className={styles.hint}>{rootHint}</p>}
      {level.folders.length > FILTER_FROM && (
        <div className={styles.filter}>
          <label className={styles.visuallyHidden} htmlFor={filterId}>
            Filtrar pastas
          </label>
          <input
            id={filterId}
            type="search"
            className={styles.filterField}
            placeholder="Filtrar pastas"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
          <span className={styles.filterCount}>
            {shown.length === level.folders.length
              ? foldersCountText(level.folders.length)
              : `${formatCount(shown.length)} de ${foldersCountText(level.folders.length)}`}
          </span>
        </div>
      )}
      {shown.length === 0 ? (
        <p className={styles.state}>Nenhuma pasta com esse nome.</p>
      ) : (
        <FolderList
          key={`${level.path}\u0000${filter}`}
          deviceId={device.id}
          label={device.label}
          level={level}
          folders={shown}
          selection={selection}
          onToggle={onToggle}
        />
      )}
    </>
  );
}

/** True once `active` has lasted `SLOW_MS`. */
function useSlow(active: boolean): boolean {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!active) {
      return;
    }
    const timer = setTimeout(() => setSlow(true), SLOW_MS);
    return () => {
      clearTimeout(timer);
      setSlow(false);
    };
  }, [active]);
  return active && slow;
}

// ---------------------------------------------------------------------------
// The action bar and the confirmation

function ActionBar({
  selection,
  blocked,
  sending,
  message,
  onClear,
  onIndex,
}: {
  selection: Selection;
  blocked: string | null;
  sending: boolean;
  message: string | null;
  onClear: () => void;
  onIndex: () => void;
}) {
  const count = selection.size;
  return (
    <footer className={styles.actionBar}>
      <span className={styles.selected} role="status">
        {count === 0
          ? "Marque as pastas que quer indexar"
          : count === 1
            ? "1 pasta selecionada"
            : `${formatCount(count)} pastas selecionadas`}
      </span>
      {message !== null && (
        <span className={styles.actionMessage} role="alert">
          {message}
        </span>
      )}
      <span className={styles.actionButtons}>
        {blocked !== null && count > 0 && (
          <span className={styles.reason}>{blocked}</span>
        )}
        <button
          type="button"
          className={styles.textButton}
          disabled={count === 0}
          onClick={onClear}
        >
          Limpar
        </button>
        <button
          type="button"
          className={styles.primary}
          disabled={count === 0 || blocked !== null || sending}
          onClick={onIndex}
        >
          {sending ? "Começando…" : "Indexar as pastas"}
        </button>
      </span>
    </footer>
  );
}

function ConfirmWholeDisk({
  device,
  onConfirm,
  onCancel,
}: {
  device: Device;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const titleId = useId();
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onCancel();
      }
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [onCancel]);
  const scanned = lastScanText(device);

  return (
    <div className={styles.scrim} onMouseDown={onCancel}>
      <div
        className={styles.confirm}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <h2 id={titleId} className={styles.confirmTitle}>
          Indexar o {device.label} inteiro?
        </h2>
        <p className={styles.confirmText}>
          Num disco com dezenas de milhares de fotos, isso leva horas. Dá para cancelar
          a qualquer momento, e o que já foi feito fica.
        </p>
        {scanned !== null && <p className={styles.confirmText}>{scanned}</p>}
        <div className={styles.confirmActions}>
          <button type="button" className={styles.textButton} onClick={onCancel}>
            Voltar
          </button>
          <button
            type="button"
            className={styles.primary}
            // The question was just asked: the answer is one key away.
            autoFocus
            onClick={onConfirm}
          >
            Indexar o disco inteiro
          </button>
        </div>
      </div>
    </div>
  );
}

/** `O último scan, de 12/03, achou 48.210 fotos.`, when there was one. */
function lastScanText(device: Device): string | null {
  const at = instant(device.last_scan_at);
  if (device.last_scan_file_count === null || at === null) {
    return null;
  }
  const count = device.last_scan_file_count;
  const photos = count === 1 ? "1 foto" : `${formatCount(count)} fotos`;
  return `O último scan, de ${dayText(at)}, achou ${photos}.`;
}
