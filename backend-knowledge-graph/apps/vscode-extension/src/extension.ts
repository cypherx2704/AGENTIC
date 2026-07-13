import { execFile } from "node:child_process";
import * as path from "node:path";
import * as vscode from "vscode";

interface Endpoint {
  id: string;
  method: string;
  resolved_path: string;
  handler: string;
  handler_file: string;
  handler_line: number;
  confidence: string;
  partial: boolean;
}

function bkgCommand(): string {
  return vscode.workspace.getConfiguration("bkg").get<string>("command", "bkg");
}

function runBkg(root: string): Promise<Endpoint[]> {
  return new Promise((resolve, reject) => {
    execFile(bkgCommand(), ["endpoints", root, "--json"], { cwd: root, maxBuffer: 1 << 24 }, (err, stdout) => {
      if (err) {
        reject(err);
        return;
      }
      try {
        resolve(JSON.parse(stdout) as Endpoint[]);
      } catch (parseError) {
        reject(parseError as Error);
      }
    });
  });
}

class EndpointItem extends vscode.TreeItem {
  constructor(endpoint: Endpoint, root: string) {
    super(`${endpoint.method}  ${endpoint.resolved_path}`, vscode.TreeItemCollapsibleState.None);
    const marker = endpoint.partial ? "!" : endpoint.confidence === "static-certain" ? "=" : "~";
    this.description = `${endpoint.handler}  ${marker}`;
    this.tooltip =
      `${endpoint.handler} (${endpoint.handler_file}:${endpoint.handler_line})\n` +
      `confidence: ${endpoint.confidence}${endpoint.partial ? " (partial)" : ""}`;
    this.command = {
      command: "bkg.openHandler",
      title: "Open Handler",
      arguments: [path.join(root, endpoint.handler_file), endpoint.handler_line],
    };
  }
}

class EndpointsProvider implements vscode.TreeDataProvider<EndpointItem> {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private items: EndpointItem[] = [];

  constructor(private readonly root: string) {}

  refresh(): void {
    runBkg(this.root).then(
      (endpoints) => {
        this.items = endpoints.map((endpoint) => new EndpointItem(endpoint, this.root));
        this.changed.fire();
      },
      (err: Error) => {
        void vscode.window.showErrorMessage(`bkg: ${err.message}`);
        this.items = [];
        this.changed.fire();
      },
    );
  }

  getTreeItem(item: EndpointItem): vscode.TreeItem {
    return item;
  }

  getChildren(): EndpointItem[] {
    return this.items;
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    return;
  }
  const root = folder.uri.fsPath;
  const provider = new EndpointsProvider(root);
  provider.refresh();

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("bkgEndpoints", provider),
    vscode.commands.registerCommand("bkg.refresh", () => provider.refresh()),
    vscode.commands.registerCommand("bkg.openHandler", async (file: string, line: number) => {
      const doc = await vscode.workspace.openTextDocument(file);
      const editor = await vscode.window.showTextDocument(doc);
      const position = new vscode.Position(Math.max(0, line - 1), 0);
      editor.selection = new vscode.Selection(position, position);
      editor.revealRange(new vscode.Range(position, position));
    }),
    vscode.workspace.onDidSaveTextDocument((doc) => {
      if (doc.fileName.endsWith(".py")) {
        provider.refresh();
      }
    }),
  );
}

export function deactivate(): void {
  // nothing to clean up — the CLI is stateless per invocation
}
