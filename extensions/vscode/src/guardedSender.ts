// Sends messages to the webview only while the panel is alive. Once the panel is disposed, touching its webview
// throws "Webview is disposed", and the agent's output or exit can still arrive after that, so every send goes
// through this. Pure (no vscode import), so it is tested without VS Code.

export class GuardedSender<T> {
  private disposed = false;

  constructor(private readonly deliver: (message: T) => void) {}

  send(message: T): void {
    if (this.disposed) {
      return;
    }
    this.deliver(message);
  }

  dispose(): void {
    this.disposed = true;
  }
}
