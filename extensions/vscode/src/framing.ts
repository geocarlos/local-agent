// JSON lines framing for `local-agent serve --stdio`: one JSON-RPC message per line, UTF-8, "\n" ends a line.
// Pure (no vscode import) so it can be tested with the plain node test runner.

export class LineSplitter {
  private pending = "";

  // Adds a chunk of text and returns the complete lines in it (without "\n"). A partial line waits for the next chunk.
  push(chunk: string): string[] {
    const parts = (this.pending + chunk).split("\n");
    this.pending = parts.pop() ?? "";
    return parts.map((line) => (line.endsWith("\r") ? line.slice(0, -1) : line)).filter((line) => line.length > 0);
  }

  // Returns the unfinished last line (if any) once the stream has ended, and clears it.
  end(): string[] {
    const rest = this.pending;
    this.pending = "";
    return rest.length > 0 ? [rest] : [];
  }
}

// One message as a line. The text must not contain "\n" (JSON.stringify output never does).
export function encodeLine(message: string): string {
  if (message.includes("\n")) {
    throw new Error("A protocol message must be one line.");
  }
  return message + "\n";
}
