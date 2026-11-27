import { execFile } from "node:child_process";
import { constants } from "node:fs";
import { access, copyFile, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";

const execute = promisify(execFile);

async function findBrowser() {
  const programs = [process.env.ProgramFiles, process.env["ProgramFiles(x86)"]].filter(Boolean);
  const pathDirs = (process.env.PATH || "").split(delimiter);
  const candidates = process.env.CHROME_BIN ? [process.env.CHROME_BIN] : [
    ...programs.flatMap((root) => [
      join(root, "Google", "Chrome", "Application", "chrome.exe"),
      join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
    ]),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ...pathDirs.flatMap((dir) => ["google-chrome", "chromium", "chromium-browser"].map((exe) => join(dir, exe))),
  ];
  for (const candidate of candidates) {
    try {
      await access(candidate, constants.X_OK);
      return candidate;
    } catch {
      // Try the next installed browser.
    }
  }
  throw new Error("Install Chrome, Chromium, or Edge, or set CHROME_BIN to its executable.");
}

// Both functions execute in the browser and must be self-contained. The setup
// runs before app.js. The async scenario runs after the dashboard initializes.
export async function runDashboardBrowserTest(setup, scenario) {
  const browser = await findBrowser();
  const dir = await mkdtemp(join(tmpdir(), "royal-dashboard-test-"));
  try {
    const source = new URL("../static/", import.meta.url);
    const html = await readFile(new URL("index.html", source), "utf8");
    await Promise.all([
      copyFile(new URL("app.js", source), join(dir, "app.js")),
      copyFile(new URL("styles.css", source), join(dir, "styles.css")),
      writeFile(join(dir, "index.html"), html
        .replace("</head>", '<script src="./fixture.js"></script></head>')
        .replace("</body>", '<script src="./scenario.js" defer></script></body>')),
      writeFile(join(dir, "fixture.js"), `(${setup})();`),
      writeFile(join(dir, "scenario.js"), `
        (async () => {
          try {
            await (${scenario})();
            document.documentElement.dataset.testResult = "passed";
          } catch (error) {
            document.documentElement.dataset.testResult = encodeURIComponent(String(error.stack || error));
          }
        })();`),
    ]);
    const args = [
      "--headless", "--disable-gpu", "--disable-extensions", "--disable-background-networking",
      "--no-first-run", "--no-default-browser-check", "--disable-sync",
      `--user-data-dir=${join(dir, "profile")}`, "--virtual-time-budget=5000", "--dump-dom",
      pathToFileURL(join(dir, "index.html")).href,
    ];
    const { stdout } = await execute(browser, args, { timeout: 45_000, maxBuffer: 2_000_000 });
    const result = /data-test-result="([^"]+)"/.exec(stdout)?.[1];
    if (result !== "passed") throw new Error(result ? decodeURIComponent(result) : "Browser scenario did not finish.");
  } finally {
    await rm(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
}
