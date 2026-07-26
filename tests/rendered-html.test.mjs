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
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.equal(response.headers.get("referrer-policy"), "no-referrer");
  assert.equal(response.headers.get("x-frame-options"), "SAMEORIGIN");
  assert.equal(
    response.headers.get("permissions-policy"),
    "camera=(), geolocation=(), microphone=()",
  );

  const html = await response.text();
  assert.doesNotMatch(html, developmentPreviewMeta);
  assert.match(html, /<title>LocalForge — RTX 5090 로컬 3D 스튜디오<\/title>/i);
  assert.match(html, /LOCALFORGE/);
  assert.match(html, /이미지를/);
  assert.match(html, /3D 생성 시작/);
  assert.match(html, /CHECKING ENGINE/);
  assert.match(html, /출력 파일 무결성/);
  assert.match(html, /SHA-256/);
  assert.match(html, /MANIFEST/);
  assert.doesNotMatch(
    html,
    /Building your site|Your site is taking shape|react-loading-skeleton/i,
  );
});

test("keeps the starter preview removed and LocalForge wired", async () => {
  const [page, styles, layout, worker, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../worker/index.ts", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(page, /NEXT_PUBLIC_LOCALFORGE_API/);
  assert.match(page, /NEXT_PUBLIC_LOCALFORGE_API_TOKEN/);
  assert.match(page, /X-LocalForge-Token/);
  assert.match(page, /"Idempotency-Key": idempotencyKey/);
  assert.match(page, /crypto\.randomUUID\(\)/);
  assert.match(page, /while \(!signal\.aborted\)/);
  assert.match(page, /response\.status === 408/);
  assert.match(page, /consecutiveFailures/);
  assert.doesNotMatch(page, /attempt < 240/);
  assert.ok(
    (page.match(/headers: API_HEADERS/g) ?? []).length >= 4,
    "health, create, poll, cancel requests should all use authenticated headers",
  );
  assert.match(page, /@google\/model-viewer/);
  assert.match(page, /className="studio-shell"/);
  assert.match(page, /3D 생성 시작/);
  assert.match(page, /\| "cancelled"/);
  assert.match(page, /method: "DELETE"/);
  assert.match(page, /readApiError/);
  assert.match(page, /output_sha256/);
  assert.match(page, /output_bytes/);
  assert.match(page, /manifest_sha256/);
  assert.match(page, /manifest_url/);
  assert.match(page, /crypto\.subtle\.digest\("SHA-256"/);
  assert.match(page, /X-Checksum-SHA256/);
  assert.match(page, /GLB SHA-256이 서버 기록과 일치하지 않습니다/);
  assert.match(page, /manifest\.output\?\.sha256 !== outputHash/);
  assert.match(page, /manifestHash !== completedJob\.manifest_sha256/);
  assert.match(page, /resolvedUrl\.origin === apiUrl\.origin/);
  assert.match(page, /mode === "image" && file/);
  assert.match(page, /mode === "text" \? prompt\.trim\(\) : ""/);
  assert.match(page, /if \(isWorking\)/);
  assert.ok(
    (page.match(/disabled=\{isWorking\}/g) ?? []).length >= 7,
    "generation inputs should remain immutable while a request is active",
  );
  assert.match(page, /download=\{`localforge-\$\{job\.id\}\.glb`\}/);
  assert.match(page, /URL\.createObjectURL/);
  assert.match(page, /input_sha256/);
  assert.match(page, /created_at/);
  assert.match(page, /updated_at/);
  assert.match(page, /무결성 검증 완료/);
  assert.match(page, /매니페스트 JSON/);
  assert.match(page, /aria-label="현재 3D 생성 작업 취소"/);
  assert.match(page, /role="progressbar"/);
  assert.match(styles, /\.result-card/);
  assert.match(styles, /\.cancel-button/);
  assert.match(styles, /@media \(max-width: 380px\)/);
  assert.match(layout, /LocalForge — RTX 5090 로컬 3D 스튜디오/);
  assert.match(layout, /generateMetadata/);
  assert.match(layout, /\/og\.png/);
  assert.match(worker, /withSecurityHeaders/);
  assert.match(worker, /X-Content-Type-Options/);
  assert.doesNotMatch(packageJson, /drizzle/);
  assert.doesNotMatch(
    `${page}\n${styles}\n${layout}\n${worker}\n${packageJson}`,
    /codex-preview|_sites-preview|react-loading-skeleton/,
  );

  await assert.rejects(access(previewRoot));
});
