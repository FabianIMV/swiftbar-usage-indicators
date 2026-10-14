// Regenerates the README images from docs/demo/index.html.
//
// Usage:
//   npm install --no-save playwright && npx playwright install chromium
//   node scripts/screenshots.mjs

import { chromium } from "playwright";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pageUrl = pathToFileURL(path.join(root, "docs/demo/index.html")).href;
const outDir = path.join(root, "docs/images");

const shots = [
  { id: "menubar", file: "menubar.png" },
  { id: "dropdown-claude", file: "dropdown-claude.png" },
  { id: "dropdown-copilot", file: "dropdown-copilot.png" },
  { id: "ramps", file: "color-ramps.png" },
];

const browser = await chromium.launch();
const page = await browser.newPage({ deviceScaleFactor: 2, viewport: { width: 960, height: 1400 } });
await page.goto(pageUrl);
await page.evaluate(() => document.fonts.ready);

for (const { id, file } of shots) {
  await page.locator(`#${id}`).screenshot({ path: path.join(outDir, file), omitBackground: true });
  console.log(`wrote docs/images/${file}`);
}

await browser.close();
