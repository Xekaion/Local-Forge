import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const developmentPreviewMeta =
  /<meta(?=[^>]*\bname=["']codex-preview["'])(?=[^>]*\bcontent=["']development["'])[^>]*>/i;
const previewRoot = new URL("../app/_sites-preview/", import.meta.url);

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the LocalForge studio", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.doesNotMatch(html, developmentPreviewMeta);
  assert.match(html, /<title>LocalForge — RTX 5090 로컬 3D 스튜디오<\/title>/i);
  assert.match(html, /LOCALFORGE/);
  assert.match(html, /이미지를/);
  assert.match(html, /3D 생성 시작/);
  assert.match(html, /CHECKING ENGINE/);
  assert.doesNotMatch(
    html,
    /Building your site|Your site is taking shape|react-loading-skeleton/i,
  );
});

test("keeps the starter preview removed and LocalForge wired", async () => {
  const [page, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(page, /NEXT_PUBLIC_LOCALFORGE_API/);
  assert.match(page, /@google\/model-viewer/);
  assert.match(page, /className="studio-shell"/);
  assert.match(page, /3D 생성 시작/);
  assert.match(layout, /LocalForge — RTX 5090 로컬 3D 스튜디오/);
  assert.match(layout, /generateMetadata/);
  assert.match(layout, /\/og\.png/);
  assert.doesNotMatch(
    `${page}\n${layout}\n${packageJson}`,
    /codex-preview|_sites-preview|react-loading-skeleton/,
  );

  await assert.rejects(access(previewRoot));
});
