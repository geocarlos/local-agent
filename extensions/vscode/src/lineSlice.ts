// The `line` and `limit` of fs/read_text_file (ACP v1): `line` is the 1-based line to start at and `limit` the number of
// lines to return. Pure, so it is tested without VS Code.

// The part of `text` from line `line` (1-based, default 1) for `limit` lines (default: to the end). Lines keep their
// own endings; a final newline is kept when the text had one and the slice reaches the end.
export function sliceLines(text: string, line?: number | null, limit?: number | null): string {
  const lines = text.split("\n");
  const endsWithNewline = text.endsWith("\n");
  if (endsWithNewline) {
    lines.pop(); // the empty piece after the last newline is not a line
  }
  const start = line == null ? 0 : Math.min(Math.max(line - 1, 0), lines.length);
  const end = limit == null ? lines.length : Math.min(start + limit, lines.length);
  const picked = lines.slice(start, end);
  if (picked.length === 0) {
    return "";
  }
  const reachesEnd = end === lines.length;
  return picked.join("\n") + (reachesEnd && !endsWithNewline ? "" : "\n");
}
