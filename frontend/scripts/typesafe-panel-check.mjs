// Drives the TypeSafe block in the agent panel against a *running* server, so the check covers what
// the browser actually does: which key the panel says it would use, and whether the test button
// makes a real judgment call. The server is the caller's job -- this script only looks at a URL.
//
//   BASE_URL=http://127.0.0.1:8123 node scripts/typesafe-panel-check.mjs
//
// The key is read from the server's environment by the server itself; nothing here needs one, and
// nothing here prints one.
import { chromium } from "@playwright/test";

const base = process.env.BASE_URL ?? "http://127.0.0.1:8123";
const screenshot = process.env.SCREENSHOT ?? "";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
page.on("pageerror", (error) => consoleErrors.push(String(error)));

await page.goto(base, { waitUntil: "networkidle" });
await page.getByRole("tab", { name: "Agent" }).click();
await page.locator("details.agent-provider-settings summary").click();

const keySource = page.locator(".typesafe-key-source").first();
await keySource.waitFor({ timeout: 15000 });
const keySourceClass = (await keySource.getAttribute("class")) ?? "";
const keySourceText = await keySource.innerText();
console.log("key source class:", keySourceClass);
console.log("key source text :", keySourceText.slice(0, 200));

const keyInput = page.locator(".typesafe-settings input[type=password], .typesafe-settings input[type=text]").first();
const placeholder = await keyInput.getAttribute("placeholder");
console.log("key placeholder :", placeholder);

await page.getByRole("button", { name: "测试 TypeSafe Key" }).click();
const status = page.locator(".typesafe-settings .provider-model-status").first();
await status.waitFor({ timeout: 60000 });
const statusText = await status.innerText();
console.log("test text       :", statusText);

if (screenshot) await page.screenshot({ path: screenshot });
await browser.close();

// A panel that cannot say where its key comes from is the failure this check exists for, so the
// assertions are about that statement and the live answer -- not about the page merely rendering.
const failures = [];
if (!keySourceClass.includes("typesafe-key-source-")) failures.push(`key source has no tone: ${keySourceClass}`);
if (/正在询问服务端的 TypeSafe 配置/.test(keySourceText)) failures.push("the panel never learned the server's answer");
if (/没有 TypeSafe Key/.test(keySourceText) && placeholder?.includes("留空")) {
  failures.push("the panel offers the server's key while also saying there is none");
}
if (!/^Key 有效/.test(statusText)) failures.push(`the live verify call did not succeed: ${statusText}`);
if (consoleErrors.length) failures.push(`console errors: ${consoleErrors.join(" | ")}`);

console.log(failures.length ? `FAIL: ${failures.join("; ")}` : "PASS — panel reports its key source and the test button returned a real judgment");
process.exit(failures.length ? 1 : 0);
