import type { ILink, ILinkProvider, IBufferCellPosition, Terminal as XTerm } from "@xterm/xterm";

// Match http(s):// or www. URLs without whitespace or the few obvious wrappers.
// Trailing punctuation handled separately so we don't eat the period at the
// end of a sentence that contains a URL.
const URL_REGEX = /(https?:\/\/[^\s<>"'`]+|www\.[^\s<>"'`]+)/gi;

// Strip trailing chars that are almost never part of an intended URL but
// commonly appear next to one (sentence punctuation, closing quotes, an
// unbalanced trailing paren when the URL was wrapped in `(...)`).
function trimTrailingPunctuation(url: string): string {
  let trimmed = url;
  while (trimmed.length > 1 && /[.,;:!?'"`]$/.test(trimmed)) {
    trimmed = trimmed.slice(0, -1);
  }
  const opens = (trimmed.match(/\(/g) || []).length;
  const closes = (trimmed.match(/\)/g) || []).length;
  if (closes > opens && trimmed.endsWith(")")) trimmed = trimmed.slice(0, -1);
  return trimmed;
}

type Activator = (event: MouseEvent, uri: string) => void;

/**
 * xterm.js link provider that handles URLs wrapped across multiple buffer
 * lines. The stock `@xterm/addon-web-links` only matches per-line, so any
 * URL long enough to wrap renders as two broken half-links (or none).
 *
 * Strategy: for the row the user is hovering, walk back/forward across
 * buffer lines marked `isWrapped` to reconstruct the full logical line as
 * a single string while keeping a parallel array mapping each character
 * back to its (col, row) cell. Run the URL regex over the reconstructed
 * string, then map matched substring indices back to {x, y} ranges xterm
 * can highlight.
 */
class WrappedLinkProvider implements ILinkProvider {
  constructor(private readonly term: XTerm, private readonly activate: Activator) {}

  provideLinks(bufferLineNumber: number, callback: (links: ILink[] | undefined) => void): void {
    const buffer = this.term.buffer.active;
    // bufferLineNumber arrives 1-based per xterm's API; buffer.getLine is 0-based.
    const targetRow0 = bufferLineNumber - 1;

    // Walk back to find the first row of the logical line: a row N is a
    // continuation of N-1 iff getLine(N).isWrapped. So while the CURRENT
    // row's isWrapped is true, step back.
    let startRow0 = targetRow0;
    while (startRow0 > 0 && buffer.getLine(startRow0)?.isWrapped) {
      startRow0--;
    }
    // Walk forward to find the last row.
    let endRow0 = startRow0;
    while (true) {
      const next = buffer.getLine(endRow0 + 1);
      if (!next || !next.isWrapped) break;
      endRow0++;
    }

    const cols = this.term.cols;
    let fullText = "";
    const charPositions: IBufferCellPosition[] = [];
    for (let r0 = startRow0; r0 <= endRow0; r0++) {
      const line = buffer.getLine(r0);
      if (!line) continue;
      for (let c = 0; c < cols; c++) {
        const cell = line.getCell(c);
        if (!cell) continue;
        // Trailing cell of a double-wide char reports width 0 — skip it so
        // we don't double-emit the glyph or misalign positions.
        if (cell.getWidth() === 0) continue;
        const ch = cell.getChars() || " ";
        fullText += ch;
        charPositions.push({ x: c + 1, y: r0 + 1 });
      }
    }

    const links: ILink[] = [];
    for (const match of fullText.matchAll(URL_REGEX)) {
      const trimmed = trimTrailingPunctuation(match[0]);
      if (!trimmed) continue;
      const startIdx = match.index ?? 0;
      const endIdx = startIdx + trimmed.length - 1;
      const startPos = charPositions[startIdx];
      const endPos = charPositions[endIdx];
      if (!startPos || !endPos) continue;
      // xterm queries link providers per row. We reconstructed the entire
      // logical line, so filter to matches that actually intersect the
      // hovered row — otherwise we'd return the same link N times for an
      // N-row wrapped URL.
      if (startPos.y > bufferLineNumber || endPos.y < bufferLineNumber) continue;
      const uri = trimmed.toLowerCase().startsWith("www.") ? `https://${trimmed}` : trimmed;
      links.push({
        range: { start: startPos, end: endPos },
        text: trimmed,
        activate: (event) => this.activate(event, uri),
      });
    }
    callback(links.length > 0 ? links : undefined);
  }
}

export function registerWrappedLinks(term: XTerm, activate: Activator) {
  return term.registerLinkProvider(new WrappedLinkProvider(term, activate));
}
