import type { IBufferLine, ILink, ILinkProvider, IBufferCellPosition, Terminal as XTerm } from "@xterm/xterm";

// Match http(s):// or www. URLs without whitespace or the few obvious wrappers.
// Trailing punctuation handled separately so we don't eat the period at the
// end of a sentence that contains a URL.
const URL_REGEX = /(https?:\/\/[^\s<>"'`]+|www\.[^\s<>"'`]+)/gi;

// Characters allowed inside a URL body (RFC 3986 reserved + unreserved, minus
// whitespace and the few brackets we reject for boundary heuristics). Used to
// decide whether a hard newline should be treated as a continuation point —
// if line N ends with one of these and line N+1 starts with one, the URL
// almost certainly bridges the boundary even though xterm didn't flag the
// next line as `isWrapped`.
const URL_BODY_CHAR = /[A-Za-z0-9\-._~:/?#@!$&*+,;=%]/;

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

/** Last non-whitespace, non-empty character on the line, or "" if none. */
function lastVisibleChar(line: IBufferLine, cols: number): string {
  for (let c = cols - 1; c >= 0; c--) {
    const cell = line.getCell(c);
    if (!cell) continue;
    if (cell.getWidth() === 0) continue;
    const ch = cell.getChars();
    if (ch && ch.trim().length > 0) return ch;
  }
  return "";
}

/** First non-whitespace, non-empty character on the line, or "" if none. */
function firstVisibleChar(line: IBufferLine, cols: number): string {
  for (let c = 0; c < cols; c++) {
    const cell = line.getCell(c);
    if (!cell) continue;
    if (cell.getWidth() === 0) continue;
    const ch = cell.getChars();
    if (ch && ch.trim().length > 0) return ch;
  }
  return "";
}

/**
 * Decide whether `next` is a continuation of `prev` for URL-detection
 * purposes. xterm sets `isWrapped` only when IT did the wrapping (auto-wrap
 * at terminal width); URLs split by an emitter that injected an explicit
 * newline mid-URL won't have that flag. Falls back to a content heuristic:
 * line N ends with a URL-body character and line N+1 begins with one.
 */
function isContinuation(prev: IBufferLine, next: IBufferLine, cols: number): boolean {
  if (next.isWrapped) return true;
  const last = lastVisibleChar(prev, cols);
  if (!last || !URL_BODY_CHAR.test(last)) return false;
  const first = firstVisibleChar(next, cols);
  return Boolean(first) && URL_BODY_CHAR.test(first);
}

/**
 * xterm.js link provider that handles URLs wrapped across multiple buffer
 * lines. The stock `@xterm/addon-web-links` only matches per-line, so any
 * URL long enough to wrap renders as two broken half-links (or none).
 *
 * Strategy: for the row the user is hovering, walk back/forward across
 * continuation buffer lines (xterm-wrapped or URL-shaped hard-newline
 * splits) to reconstruct the full logical line as a single string while
 * keeping a parallel array mapping each character back to its (col, row)
 * cell. Run the URL regex over the reconstructed string, then map matched
 * substring indices back to {x, y} ranges xterm can highlight.
 */
class WrappedLinkProvider implements ILinkProvider {
  constructor(private readonly term: XTerm, private readonly activate: Activator) {}

  provideLinks(bufferLineNumber: number, callback: (links: ILink[] | undefined) => void): void {
    const buffer = this.term.buffer.active;
    // bufferLineNumber arrives 1-based per xterm's API; buffer.getLine is 0-based.
    const targetRow0 = bufferLineNumber - 1;

    const cols = this.term.cols;
    // Walk back to find the first row of the logical line: a row N is a
    // continuation of N-1 if xterm flagged it `isWrapped` OR a URL-shape
    // heuristic suggests they bridge a hard-newline split.
    let startRow0 = targetRow0;
    while (startRow0 > 0) {
      const prev = buffer.getLine(startRow0 - 1);
      const cur = buffer.getLine(startRow0);
      if (!prev || !cur) break;
      if (!isContinuation(prev, cur, cols)) break;
      startRow0--;
    }
    // Walk forward to find the last row.
    let endRow0 = startRow0;
    while (true) {
      const next = buffer.getLine(endRow0 + 1);
      const cur = buffer.getLine(endRow0);
      if (!next || !cur) break;
      if (!isContinuation(cur, next, cols)) break;
      endRow0++;
    }

    let fullText = "";
    const charPositions: IBufferCellPosition[] = [];
    for (let r0 = startRow0; r0 <= endRow0; r0++) {
      const line = buffer.getLine(r0);
      if (!line) continue;
      // Find the last visible cell so we don't emit a tail of spaces from
      // trailing empties — that tail would break a hard-newline-split URL
      // by inserting whitespace between "…/very-" and the continuation.
      let lastVisibleCol = -1;
      for (let c = cols - 1; c >= 0; c--) {
        const cell = line.getCell(c);
        if (!cell || cell.getWidth() === 0) continue;
        const ch = cell.getChars();
        if (ch && ch.trim().length > 0) {
          lastVisibleCol = c;
          break;
        }
      }
      if (lastVisibleCol < 0) continue;
      // On continuation rows reached via the URL-shape heuristic (hard
      // newline + optional indent), leading whitespace is layout-only —
      // skip it. Don't trim leading whitespace on the first row because
      // it might be genuine indentation around a URL embedded in prose.
      let skippingLeading = r0 !== startRow0;
      for (let c = 0; c <= lastVisibleCol; c++) {
        const cell = line.getCell(c);
        if (!cell) continue;
        // Trailing cell of a double-wide char reports width 0 — skip it so
        // we don't double-emit the glyph or misalign positions.
        if (cell.getWidth() === 0) continue;
        const ch = cell.getChars() || " ";
        if (skippingLeading) {
          if (ch.trim().length === 0) continue;
          skippingLeading = false;
        }
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
