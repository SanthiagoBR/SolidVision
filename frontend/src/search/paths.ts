/**
 * Where a photo is, as the screen says it (RFC-038 sections 2.8 and 5.4).
 *
 * The API publishes paths with `/` on every platform (RFC-030), and the
 * screen shows them as Windows does, with `\`. `filename` in the response has
 * no extension, so the card's name is the last segment of `relative_path`:
 * `DJI_0042.JPG`, as Explorer shows it.
 */

export interface Located {
  readonly relative_path: string;
  readonly absolute_path: string | null;
  readonly device: { readonly label: string; readonly connected: boolean };
}

export function windowsPath(path: string): string {
  return path.replaceAll("/", "\\");
}

/** `fotos/2018/junho/DJI_0042.JPG` gives `DJI_0042.JPG`. */
export function fileName(relativePath: string): string {
  const parts = relativePath.split("/");
  return parts[parts.length - 1] ?? relativePath;
}

/** `fotos/2018/junho/DJI_0042.JPG` gives `fotos/2018/junho`; a file at the
 * root of the disk has no folder. */
export function folderOf(relativePath: string): string {
  const cut = relativePath.lastIndexOf("/");
  return cut < 0 ? "" : relativePath.slice(0, cut);
}

/** With the disk here, the absolute path; else `HD3 · fotos\2018\DJI_0042.JPG`. */
export function displayPath(image: Located): string {
  if (image.device.connected && image.absolute_path !== null) {
    return windowsPath(image.absolute_path);
  }
  return `${image.device.label} · ${windowsPath(image.relative_path)}`;
}

/** What "Copiar caminho" copies: the absolute path, or the relative one. */
export function copyablePath(image: Located): string {
  if (image.device.connected && image.absolute_path !== null) {
    return windowsPath(image.absolute_path);
  }
  return windowsPath(image.relative_path);
}

/** The button: "Copiar caminho", or "Copiar caminho no HD3" when the disk is
 * away and what is copied is the path inside it. */
export function copyLabel(image: Located): string {
  return image.device.connected && image.absolute_path !== null
    ? "Copiar caminho"
    : `Copiar caminho no ${image.device.label}`;
}

/** `F:/fotos/2018/junho/DJI_0042.JPG` gives `F:\fotos\2018\junho`. */
export function parentFolder(path: string): string {
  return windowsPath(folderOf(path));
}
