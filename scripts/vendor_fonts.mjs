import { copyFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const root = process.cwd();
const destination = join(root, "gamgui", "web", "static", "fonts");
mkdirSync(destination, { recursive: true });

const assets = [
  ["@fontsource/source-sans-3/files", "source-sans-3-latin-300-normal.woff2"],
  ["@fontsource/source-sans-3/files", "source-sans-3-latin-400-normal.woff2"],
  ["@fontsource/source-sans-3/files", "source-sans-3-latin-500-normal.woff2"],
  ["@fontsource/source-sans-3/files", "source-sans-3-latin-600-normal.woff2"],
  ["@fontsource/source-sans-3/files", "source-sans-3-latin-400-italic.woff2"],
  ["@fontsource/source-serif-4/files", "source-serif-4-latin-400-normal.woff2"],
  ["@fontsource/source-serif-4/files", "source-serif-4-latin-600-normal.woff2"],
  ["@fontsource/source-serif-4/files", "source-serif-4-latin-400-italic.woff2"],
  ["@fontsource/source-sans-3", "LICENSE", "OFL-source-sans-3.txt"],
  ["@fontsource/source-serif-4", "LICENSE", "OFL-source-serif-4.txt"]
];

for (const [packagePath, sourceName, targetName = sourceName] of assets) {
  copyFileSync(join(root, "node_modules", packagePath, sourceName), join(destination, targetName));
}

process.stdout.write("Vendored Source Sans 3 and Source Serif 4 font assets.\n");
