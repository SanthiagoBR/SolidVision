/**
 * The bar, the pills and the active filters (RFC-038 sections 5.2 and 5.5),
 * the same on the home screen and on the search screen.
 *
 * - **The search runs on Enter**, or on a click on an example query, and not
 *   on every key: a query in Portuguese costs the translation and CLIP, 348 ms
 *   in the backend (RFC-035 section 15).
 * - **A photo can be dropped on the bar**, or chosen with "Buscar por foto…".
 *   JPEG and PNG up to 50 MB; anything else is not sent, and says why.
 * - **"Dispositivos"** lists every device, connected or not. None checked is
 *   all of them; all checked is the same, and neither sends a `device_id`.
 * - **"Filtros"** holds the dates -- both inclusive, either optional -- and the
 *   "perto de", which is born from a photo (section 5.4) or, since RFC-040,
 *   from a cell of the map: *"Até 10 km do ponto do mapa"*.
 * - **The active filters** are dark pills with an ×, as in the Figma.
 *
 * Since RFC-040 (section 5.5) the pills are `FilterPills`, which the map uses
 * without the bar and without the "perto de": the map's area is the visible
 * one. Their rules did not change.
 *
 * With search `unavailable` the bar and the photo are disabled, with the
 * `search_detail`; "Parecidas com esta" still works, because it needs no
 * model.
 */

import type { DragEvent, KeyboardEvent, ReactNode } from "react";
import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router";

import type { Device } from "../api/client";
import { useDevices, useHealth } from "../api/queries";
import { searchStatusText } from "../home/search-status";
import { startMapGesture } from "../map/timing";
import { mapHrefFor } from "../map/url";
import { useCapabilities, useImage } from "./api";
import { dateFilterText, isInverted } from "./dates";
import type { DroppedPhoto } from "./dropped-photo";
import { uploadRefusal, UPLOAD_TYPES } from "./messages";
import { devicePillText, type Filters, type SearchQuery, toggleDevice } from "./params";
import { fileName } from "./paths";
import {
  effectiveRadius,
  formatRadius,
  nearFilterText,
  nearTitleText,
  radiusOptions,
} from "./radius";
import styles from "./SearchControls.module.css";

export const PHOTO_PROMPT = "Solte a foto para buscar parecidas";
export const TEXT_PROMPT = "Descreva a foto que você procura…";
/** The bar's text when the home screen opens with the circle of a cell of the
 * map, and no search (RFC-040 section 5.4). */
export const MAP_POINT_PROMPT = "Descreva o que procurar perto do ponto do mapa…";

export interface SearchControlsProps {
  /** The text in the bar when the screen opens: the current search's. */
  readonly text: string;
  /** The current search, which "ou escolha um lugar no mapa" carries. */
  readonly query: SearchQuery | null;
  readonly filters: Filters;
  /** The dropped photo, when the search is one. */
  readonly photo: DroppedPhoto | null;
  readonly onText: (query: string) => void;
  readonly onPhoto: (file: File) => void;
  readonly onClearPhoto: () => void;
  readonly onFilters: (filters: Filters) => void;
  /** Right of the pills: "14 resultados · 0,13 s". */
  readonly summary?: ReactNode;
  /** The circle came from a cell of the map, with no search: the bar has the
   * focus, and its text says what it is for. */
  readonly fromMapPoint?: boolean;
}

export function SearchControls(props: SearchControlsProps) {
  return (
    <div className={styles.controls}>
      <SearchBar key={props.text} {...props} />
      <FilterPills
        filters={props.filters}
        onFilters={props.onFilters}
        mapHref={mapHrefFor(props.query, props.filters)}
        summary={props.summary}
      />
    </div>
  );
}

export interface FilterPillsProps {
  readonly filters: Filters;
  readonly onFilters: (filters: Filters) => void;
  /** The search's pills: "Perto de", with a link to the map with this search
   * and its filters in its note. The map's pills have no "Perto de". */
  readonly mapHref?: string;
  readonly summary?: ReactNode;
}

/** "Dispositivos", "Filtros" and the active filters (RFC-040 section 5.5). */
export function FilterPills({
  filters,
  onFilters,
  mapHref,
  summary,
}: FilterPillsProps) {
  return (
    <div className={styles.pills}>
      <DevicesPill filters={filters} onFilters={onFilters} />
      <FiltersPill filters={filters} onFilters={onFilters} mapHref={mapHref} />
      <ActiveFilters filters={filters} onFilters={onFilters} />
      {summary !== undefined && (
        <span className={styles.summary} role="status">
          {summary}
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The bar

function SearchBar({
  text,
  photo,
  onText,
  onPhoto,
  onClearPhoto,
  fromMapPoint = false,
}: SearchControlsProps) {
  const health = useHealth();
  const [draft, setDraft] = useState(text);
  const [dragging, setDragging] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const unavailable = health.data?.search === "unavailable";
  const status = refusal ?? searchStatusText(health.data);

  const take = (file: File | undefined) => {
    setDragging(false);
    if (file === undefined || unavailable) {
      return;
    }
    const refused = uploadRefusal(file);
    setRefusal(refused);
    if (refused === null) {
      onPhoto(file);
    }
  };

  const submit = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter") {
      return;
    }
    event.preventDefault();
    const query = draft.trim();
    if (query !== "") {
      setRefusal(null);
      onText(query);
    }
  };

  const over = (event: DragEvent) => {
    event.preventDefault();
    if (!unavailable) {
      event.dataTransfer.dropEffect = "copy";
      setDragging(true);
    }
  };
  const leave = (event: DragEvent) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
      setDragging(false);
    }
  };
  const drop = (event: DragEvent) => {
    event.preventDefault();
    take(event.dataTransfer.files[0]);
  };

  return (
    <div
      className={dragging ? `${styles.bar} ${styles.dragging}` : styles.bar}
      onDragEnter={over}
      onDragOver={over}
      onDragLeave={leave}
      onDrop={drop}
    >
      <span className={styles.icon} aria-hidden="true">
        ⌕
      </span>
      {dragging ? (
        <span className={styles.dropPrompt}>{PHOTO_PROMPT}</span>
      ) : photo !== null ? (
        <span className={styles.photo}>
          <img className={styles.preview} src={photo.previewUrl} alt="" />
          <span className={styles.photoName}>{photo.file.name}</span>
          <button
            type="button"
            className={styles.clear}
            onClick={onClearPhoto}
            aria-label="Tirar a foto e voltar ao início"
          >
            ×
          </button>
        </span>
      ) : (
        <input
          className={styles.input}
          type="search"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={submit}
          placeholder={fromMapPoint ? MAP_POINT_PROMPT : TEXT_PROMPT}
          aria-label={(fromMapPoint ? MAP_POINT_PROMPT : TEXT_PROMPT).replace("…", "")}
          autoFocus={fromMapPoint}
          disabled={unavailable}
          enterKeyHint="search"
        />
      )}
      {status !== null && !dragging && (
        <span className={styles.status} role="status" title={status}>
          {status}
        </span>
      )}
      <button
        type="button"
        className={styles.byPhoto}
        onClick={() => picker.current?.click()}
        disabled={unavailable}
      >
        Buscar por foto…
      </button>
      <input
        ref={picker}
        className={styles.picker}
        type="file"
        accept={UPLOAD_TYPES.join(",")}
        tabIndex={-1}
        aria-hidden="true"
        onChange={(event) => {
          take(event.target.files?.[0]);
          event.target.value = "";
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// The pills

function usePopover() {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) {
      return;
    }
    const outside = (event: MouseEvent) => {
      if (root.current !== null && !root.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const escape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", outside);
    document.addEventListener("keydown", escape, true);
    return () => {
      document.removeEventListener("mousedown", outside);
      document.removeEventListener("keydown", escape, true);
    };
  }, [open]);
  return { open, setOpen, root };
}

function DevicesPill({
  filters,
  onFilters,
}: {
  filters: Filters;
  onFilters: (filters: Filters) => void;
}) {
  const devices = useDevices();
  const { open, setOpen, root } = usePopover();
  const list: readonly Device[] | null = devices.data ?? null;
  const all = (list ?? []).map((device) => device.id);
  const id = useId();

  return (
    <div className={styles.anchor} ref={root}>
      <button
        type="button"
        className={styles.pillSelected}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
      >
        {devicePillText(list, filters.devices)}
        <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className={styles.popover} id={id} role="group" aria-label="Dispositivos">
          {list === null ? (
            <p className={styles.note}>Lendo os dispositivos…</p>
          ) : list.length === 0 ? (
            <p className={styles.note}>Nenhum dispositivo registrado</p>
          ) : (
            <>
              <p className={styles.note}>Nenhum marcado busca em todos.</p>
              {list.map((device) => (
                <label key={device.id} className={styles.check}>
                  <input
                    type="checkbox"
                    checked={filters.devices.includes(device.id)}
                    onChange={() =>
                      onFilters({
                        ...filters,
                        devices: toggleDevice(filters.devices, device.id, all),
                      })
                    }
                  />
                  <span className={styles.checkLabel}>{device.label}</span>
                  <span className={styles.checkState}>
                    {device.connected ? "conectado" : "desconectado"}
                  </span>
                </label>
              ))}
              {filters.devices.length > 0 && (
                <button
                  type="button"
                  className={styles.link}
                  onClick={() => onFilters({ ...filters, devices: [] })}
                >
                  Buscar em todos
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function FiltersPill({
  filters,
  onFilters,
  mapHref,
}: {
  filters: Filters;
  onFilters: (filters: Filters) => void;
  mapHref: string | undefined;
}) {
  const { open, setOpen, root } = usePopover();
  const id = useId();
  const active = filters.from !== null || filters.to !== null || filters.near !== null;

  return (
    <div className={styles.anchor} ref={root}>
      <button
        type="button"
        className={active ? styles.pillSelected : styles.pill}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
      >
        Filtros
        <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className={styles.popover} id={id} role="group" aria-label="Filtros">
          <DatesFilter
            filters={filters}
            onFilters={(next) => {
              onFilters(next);
              setOpen(false);
            }}
          />
          {mapHref !== undefined && (
            <NearFilter filters={filters} onFilters={onFilters} mapHref={mapHref} />
          )}
        </div>
      )}
    </div>
  );
}

function DatesFilter({
  filters,
  onFilters,
}: {
  filters: Filters;
  onFilters: (filters: Filters) => void;
}) {
  const [from, setFrom] = useState(filters.from ?? "");
  const [to, setTo] = useState(filters.to ?? "");
  const inverted = isInverted(from || null, to || null);

  return (
    <fieldset className={styles.section}>
      <legend className={styles.sectionTitle}>Data de captura</legend>
      <div className={styles.dates}>
        <label className={styles.field}>
          de
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        </label>
        <label className={styles.field}>
          até
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
        </label>
      </div>
      {inverted && (
        <p className={styles.fieldError}>O “até” vem antes do “de”, e não é enviado.</p>
      )}
      <p className={styles.note}>Os dois dias entram. Fotos sem data ficam de fora.</p>
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.apply}
          onClick={() => onFilters({ ...filters, from: from || null, to: to || null })}
        >
          Aplicar
        </button>
        {(filters.from !== null || filters.to !== null) && (
          <button
            type="button"
            className={styles.link}
            onClick={() => onFilters({ ...filters, from: null, to: null })}
          >
            Tirar as datas
          </button>
        )}
      </div>
    </fieldset>
  );
}

function NearFilter({
  filters,
  onFilters,
  mapHref,
}: {
  filters: Filters;
  onFilters: (filters: Filters) => void;
  mapHref: string;
}) {
  const capabilities = useCapabilities();
  const minimum = capabilities.data?.min_radius_m ?? null;
  const name = useNearName(filters.nearOf);

  if (filters.near === null) {
    return (
      <div className={styles.section}>
        <p className={styles.sectionTitle}>Perto de</p>
        <p className={styles.note}>
          Abra uma foto com posição e escolha “Buscar perto daqui”, ou{" "}
          <Link className={styles.inlineLink} to={mapHref} onClick={startMapGesture}>
            escolha um lugar no mapa
          </Link>
          .
        </p>
      </div>
    );
  }
  return (
    <div className={styles.section}>
      <p className={styles.sectionTitle}>{nearTitleText(name)}</p>
      {minimum === null ? (
        <p className={styles.note}>Lendo o raio mínimo…</p>
      ) : (
        <label className={styles.field}>
          raio
          <select
            value={effectiveRadius(filters.radius, minimum)}
            onChange={(event) =>
              onFilters({ ...filters, radius: Number(event.target.value) })
            }
          >
            {radiusOptions(minimum).map((radius) => (
              <option key={radius} value={radius}>
                {formatRadius(radius)}
              </option>
            ))}
          </select>
        </label>
      )}
      <p className={styles.note}>
        A posição é a da aeronave, e não a do chão fotografado.
      </p>
      <button
        type="button"
        className={styles.link}
        onClick={() =>
          onFilters({ ...filters, near: null, radius: null, nearOf: null })
        }
      >
        remover
      </button>
    </div>
  );
}

/** The name of the photo that gave the centre, or a description of it while
 * it is read; `null` for a centre no photo gave -- a cell of the map's. */
function useNearName(nearOf: string | null): string | null {
  const image = useImage(nearOf);
  if (nearOf === null) {
    return null;
  }
  if (image.data !== undefined) {
    return fileName(image.data.relative_path);
  }
  return "uma foto";
}

function ActiveFilters({
  filters,
  onFilters,
}: {
  filters: Filters;
  onFilters: (filters: Filters) => void;
}) {
  const capabilities = useCapabilities();
  const name = useNearName(filters.nearOf);
  const dates = dateFilterText(
    filters.from,
    isInverted(filters.from, filters.to) ? null : filters.to,
  );
  const minimum = capabilities.data?.min_radius_m;

  return (
    <>
      {dates !== null && (
        <Chip
          label={dates}
          onRemove={() => onFilters({ ...filters, from: null, to: null })}
        />
      )}
      {filters.near !== null && minimum !== undefined && (
        <Chip
          label={nearFilterText(effectiveRadius(filters.radius, minimum), name)}
          onRemove={() =>
            onFilters({ ...filters, near: null, radius: null, nearOf: null })
          }
        />
      )}
    </>
  );
}

function Chip({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <span className={styles.chip}>
      {label}
      <button
        type="button"
        className={styles.chipRemove}
        onClick={onRemove}
        aria-label={`Tirar o filtro: ${label}`}
      >
        ×
      </button>
    </span>
  );
}
