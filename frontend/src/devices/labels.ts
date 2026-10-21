/**
 * The label of a disk: always sent, suggested, and never repeated (RFC-039
 * section 2.8).
 *
 * The `label` column has no `unique` (`device_model.py`), and a label left out
 * of the registration falls back to the volume's, and without one to the
 * identity, a GUID (`register_device.py`). Two "HD2" in the sidebar, or a card
 * named after a volume GUID, are the same failure seen from two sides. So the
 * interface always sends one, suggested, and refuses a repeated one before it
 * sends -- case and the spaces of the ends aside. The API does not change: the
 * CLI can still repeat one, and that is declared.
 */

/** How two labels are compared: the spaces of the ends out, case aside. */
export function labelKey(label: string): string {
  return label.trim().toLocaleLowerCase("pt-BR");
}

/** The existing label that `draft` repeats, if it repeats one. */
export function repeatedLabel(
  draft: string,
  existing: readonly string[],
): string | null {
  const key = labelKey(draft);
  return existing.find((label) => labelKey(label) === key) ?? null;
}

/**
 * The label a new disk's field starts with: the volume's own, or "HD" and the
 * smallest number no device uses.
 *
 * A volume label that repeats a device's is not suggested: the field would
 * start refused. Neither is a blank one.
 */
export function suggestLabel(
  volumeLabel: string | null,
  existing: readonly string[],
): string {
  const own = volumeLabel?.trim() ?? "";
  if (own !== "" && repeatedLabel(own, existing) === null) {
    return own;
  }
  const used = new Set(existing.map(labelKey));
  let number = 1;
  while (used.has(labelKey(`HD${number}`))) {
    number += 1;
  }
  return `HD${number}`;
}

export type LabelProblem =
  { readonly kind: "empty" } | { readonly kind: "repeated"; readonly text: string };

/**
 * Why a label cannot be sent, or `null` when it can. `others` are the labels
 * of the other devices: for a rename, the device's own is not among them, and
 * keeping it, or changing only its case, is no repetition.
 */
export function labelProblem(
  draft: string,
  others: readonly string[],
): LabelProblem | null {
  if (draft.trim() === "") {
    return { kind: "empty" };
  }
  const repeated = repeatedLabel(draft, others);
  if (repeated !== null) {
    return { kind: "repeated", text: `Já existe um ${repeated}` };
  }
  return null;
}
