/**
 * The address field (RFC-043): a typed address, found by an online service.
 *
 * Sent on Enter or the button and never per key -- the service's usage policy
 * forbids autocomplete. Coordinates typed into it are used here and sent
 * nowhere, as in the coordinate fields.
 */

import { useEffect, useRef, useState } from "react";

import { answered, api, ApiError, bodyOf, type Place } from "../api/client";
import { type Point, readPoint } from "./params";

export const ADDRESS_LABEL = "Buscar um endereço";
export const ADDRESS_NOTE =
  "O endereço é enviado ao OpenStreetMap (Nominatim) quando você busca. " +
  "As coordenadas não saem do computador.";
export const ATTRIBUTION = "© OpenStreetMap";

export const NOT_FOUND =
  "Nenhum lugar encontrado. Tente com a cidade e o estado, ou use as coordenadas.";
export const UNAVAILABLE =
  "A busca por endereço não respondeu. Confira a internet, ou use as coordenadas.";
export const SWITCHED_OFF = "A busca por endereço está desligada nesta instalação.";
export const NO_ANSWER =
  "Sem resposta do SolidVision. Tente de novo quando ele voltar.";
export const FAILED = "A busca por endereço falhou. Tente de novo.";

export async function geocodeAddress(
  q: string,
  signal?: AbortSignal,
): Promise<Place | null> {
  const body = bodyOf(
    await answered(api.GET("/api/v1/geocode", { params: { query: { q } }, signal })),
  );
  return body.place;
}

/** The API's `detail` is English: the field says its own, by status. */
export function addressErrorText(status: number): string {
  switch (status) {
    case 0:
      return NO_ANSWER;
    case 403:
      return SWITCHED_OFF;
    case 503:
      return UNAVAILABLE;
    default:
      return FAILED;
  }
}

export type AddressLine =
  | { readonly kind: "found"; readonly text: string }
  | { readonly kind: "problem"; readonly text: string };

export interface AddressLookup {
  readonly text: string;
  readonly pending: boolean;
  /** Under the field: what the service understood, or why nothing moved. */
  readonly line: AddressLine | null;
  readonly change: (text: string) => void;
  readonly submit: () => void;
}

export function useAddressLookup(onFound: (point: Point) => void): AddressLookup {
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const [line, setLine] = useState<AddressLine | null>(null);
  const request = useRef<AbortController | null>(null);

  useEffect(() => () => request.current?.abort(), []);

  const submit = () => {
    const typed = text.trim();
    if (typed === "" || pending) {
      return;
    }
    const point = readPoint(typed);
    if (point !== null) {
      setLine(null);
      onFound(point);
      return;
    }
    const controller = new AbortController();
    request.current = controller;
    setPending(true);
    setLine(null);
    geocodeAddress(typed, controller.signal).then(
      (place) => {
        if (controller.signal.aborted) {
          return;
        }
        setPending(false);
        if (place === null) {
          setLine({ kind: "problem", text: NOT_FOUND });
          return;
        }
        setLine({ kind: "found", text: place.label });
        onFound({ lat: place.latitude, lon: place.longitude });
      },
      (error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setPending(false);
        const status = error instanceof ApiError ? error.status : -1;
        setLine({ kind: "problem", text: addressErrorText(status) });
      },
    );
  };

  return {
    text,
    pending,
    line,
    change: (next) => {
      setText(next);
      setLine(null);
    },
    submit,
  };
}
