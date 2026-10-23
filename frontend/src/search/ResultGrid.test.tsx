/**
 * The first criterion of RFC-038 section 8: the order of the grid is the
 * response's. Rendered by `react-dom/server`, in Node, with no DOM: the
 * `data-id`s of the HTML must come out in the order of `results`, whatever
 * their similarities say.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { SearchResult } from "../api/client";
import { ResultGrid } from "./ResultGrid";
import { similarityCorner } from "./similarity";

const cosine = (result: SearchResult) => similarityCorner(result.similarity);

function result(id: string, similarity: number, connected = true): SearchResult {
  return {
    id,
    filename: `DJI_${id}`,
    similarity,
    captured_at: null,
    capture_source: null,
    latitude: null,
    longitude: null,
    position_source: null,
    device: { id: "d", label: "HD3", connected },
    relative_path: `fotos/2018/junho/DJI_${id}.JPG`,
    absolute_path: connected ? `F:/fotos/2018/junho/DJI_${id}.JPG` : null,
  };
}

/** Each card's id and the number in its corner, in the order of the HTML. */
function cards(html: string): { id: string; similarity: string }[] {
  const found = [...html.matchAll(/<li[^>]*data-id="([^"]+)"[^>]*>(.*?)<\/li>/g)];
  return found.map(([, id, body]) => {
    const number = /title="Similaridade [^"]*"[^>]*>([^<]+)</.exec(body ?? "");
    return { id: id ?? "", similarity: number?.[1] ?? "" };
  });
}

describe("the grid", () => {
  it("keeps the order of the response, out of order similarities and all", () => {
    const results = [
      result("c", 0.21),
      result("a", 0.33),
      result("d", -0.02),
      result("b", 0.33),
      result("e", 0.29),
    ];

    const html = renderToStaticMarkup(<ResultGrid results={results} corner={cosine} />);

    expect(cards(html).map((card) => card.id)).toEqual(["c", "a", "d", "b", "e"]);
  });

  it("shows on each card the similarity of that same result", () => {
    const results = [result("x", 0.3124), result("y", -0.05), result("z", 1)];

    const html = renderToStaticMarkup(<ResultGrid results={results} corner={cosine} />);

    expect(cards(html)).toEqual([
      { id: "x", similarity: "0,312" },
      { id: "y", similarity: "\u22120,050" },
      { id: "z", similarity: "1,000" },
    ]);
  });

  it("names the file by its last segment, and says when the disk is away", () => {
    const html = renderToStaticMarkup(
      <ResultGrid results={[result("q", 0.2, false)]} corner={cosine} />,
    );

    expect(html).toContain("DJI_q.JPG");
    expect(html).toContain("fotos/2018/junho");
    expect(html).toContain("HD3 · desconectado");
  });

  it("asks for every thumbnail at once, from the API", () => {
    const html = renderToStaticMarkup(
      <ResultGrid results={[result("q", 0.2)]} corner={cosine} />,
    );

    expect(html).toContain('src="/api/v1/images/q/thumbnail"');
    expect(html).toContain('loading="eager"');
  });
});
