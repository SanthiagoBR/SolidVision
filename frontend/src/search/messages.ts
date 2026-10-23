/**
 * What the screen says when a request fails (RFC-038 section 5.7).
 *
 * The API's `detail` is the domain's message, in English, and the interface is
 * PT-BR only, so the interface writes its own, by status and by route. The
 * `detail` is shown only where it is the diagnosis: a 503 of a model that does
 * not load names the model and what to do (RFC-035 section 6.4).
 *
 * A 400 on a search is a defect -- the interface sends neither a radius below
 * the minimum nor an inverted range -- so its status goes to the console, and
 * from there to `shell.log`, without the `detail`.
 */

export type Route = "search" | "similar" | "upload" | "browse" | "detail" | "reveal";

export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;
export const UPLOAD_TYPES = ["image/jpeg", "image/png"] as const;

export const WRONG_TYPE = "Só dá para buscar com uma foto JPEG ou PNG.";
export const TOO_LARGE = "A foto passa de 50 MB.";
export const UNREADABLE = "Não deu para abrir essa foto.";
export const BAD_FILTERS = "A busca não pôde ser feita com esses filtros.";
export const NOT_IN_INDEX = "Essa foto não está mais no índice.";
export const NOT_INDEXED = "Essa foto ainda não foi indexada.";
export const NO_ANSWER =
  "Sem resposta do SolidVision. A busca volta quando ele voltar.";
export const FAILED = "A busca falhou. Tente de novo.";
/** A 404 of the listing by date: the last photo of the page before -- the
 * cursor -- left the index (RFC-044 section 6.4). */
export const LIST_CHANGED = "A lista mudou desde a última página.";

export interface RevealContext {
  readonly label: string;
  /** The folder the file was in, in Windows form. */
  readonly folder: string;
}

/** The sentence for a failed request; `status` 0 is no answer at all. */
export function errorMessage(
  route: Route,
  status: number,
  detail?: string | null,
  reveal?: RevealContext,
): string {
  if (status === 0) {
    return NO_ANSWER;
  }
  if (route === "reveal") {
    if (status === 409 && reveal) {
      return `O ${reveal.label} não está conectado. Conecte o disco e tente de novo.`;
    }
    if (status === 410 && reveal) {
      return `O arquivo não está mais em ${reveal.folder}.`;
    }
    return "Não deu para abrir o Explorer.";
  }
  switch (status) {
    case 400:
      return route === "upload" ? UNREADABLE : BAD_FILTERS;
    case 404:
      if (route === "browse") {
        return LIST_CHANGED;
      }
      return route === "similar" || route === "detail" ? NOT_IN_INDEX : FAILED;
    case 409:
      return route === "similar" ? NOT_INDEXED : FAILED;
    case 413:
      return TOO_LARGE;
    case 415:
      return WRONG_TYPE;
    case 503:
      return detail || "A busca está indisponível.";
    default:
      return FAILED;
  }
}

/** A dropped file refused before it is sent: the API checks again, and it is
 * the API that decides. */
export function uploadRefusal(file: { type: string; size: number }): string | null {
  if (!(UPLOAD_TYPES as readonly string[]).includes(file.type)) {
    return WRONG_TYPE;
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return TOO_LARGE;
  }
  return null;
}
