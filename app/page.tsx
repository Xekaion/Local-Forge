"use client";

import { createElement, useEffect, useRef, useState } from "react";

type EngineState = "checking" | "online" | "offline";
type JobState =
  | "idle"
  | "submitting"
  | "queued"
  | "running"
  | "succeeded"
  | "failed";

type GenerationJob = {
  id: string;
  status: JobState;
  progress?: number;
  stage?: string;
  model_url?: string;
  error?: string;
};

const API_BASE =
  process.env.NEXT_PUBLIC_LOCALFORGE_API ?? "http://127.0.0.1:8000";
const MAX_FILE_SIZE = 20 * 1024 * 1024;
const sleep = (milliseconds: number) =>
  new Promise((resolve) => window.setTimeout(resolve, milliseconds));

export default function Home() {
  const inputRef = useRef<HTMLInputElement>(null);
  const [mode, setMode] = useState<"image" | "text">("image");
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [prompt, setPrompt] = useState("");
  const [quality, setQuality] = useState<"draft" | "studio">("studio");
  const [remesh, setRemesh] = useState(true);
  const [engine, setEngine] = useState<EngineState>("checking");
  const [engineName, setEngineName] = useState("TRELLIS.2");
  const [job, setJob] = useState<GenerationJob>({
    id: "",
    status: "idle",
    progress: 0,
  });
  const [modelUrl, setModelUrl] = useState<string | null>(null);
  const [notice, setNotice] = useState(
    "정면이 잘 보이고 배경이 단순한 이미지를 넣어주세요.",
  );

  useEffect(() => {
    void import("@google/model-viewer");
  }, []);

  useEffect(() => {
    if (!file) {
      setPreviewUrl(null);
      return;
    }
    const objectUrl = URL.createObjectURL(file);
    setPreviewUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [file]);

  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 2500);
    fetch(`${API_BASE}/health`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("engine unavailable");
        const data = (await response.json()) as { model?: string };
        setEngineName(data.model ?? "TRELLIS.2");
        setEngine("online");
      })
      .catch(() => setEngine("offline"))
      .finally(() => window.clearTimeout(timeout));
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, []);

  const acceptFile = (nextFile?: File) => {
    if (!nextFile) return;
    if (!["image/png", "image/jpeg", "image/webp"].includes(nextFile.type)) {
      setNotice("PNG, JPG, WEBP 이미지만 사용할 수 있습니다.");
      return;
    }
    if (nextFile.size > MAX_FILE_SIZE) {
      setNotice("이미지는 20MB 이하로 줄여주세요.");
      return;
    }
    setFile(nextFile);
    setModelUrl(null);
    setJob({ id: "", status: "idle", progress: 0 });
    setNotice("입력 준비 완료. 로컬 GPU로 생성할 수 있습니다.");
  };

  const pollGeneration = async (jobId: string) => {
    for (let attempt = 0; attempt < 240; attempt += 1) {
      const response = await fetch(`${API_BASE}/v1/generations/${jobId}`);
      if (!response.ok) throw new Error("작업 상태를 확인하지 못했습니다.");
      const current = (await response.json()) as GenerationJob;
      setJob(current);
      if (current.status === "succeeded" && current.model_url) {
        setModelUrl(
          current.model_url.startsWith("http")
            ? current.model_url
            : `${API_BASE}${current.model_url}`,
        );
        setNotice("완료됐습니다. 모델을 돌려보고 GLB를 내려받으세요.");
        return;
      }
      if (current.status === "failed") {
        throw new Error(current.error ?? "3D 생성에 실패했습니다.");
      }
      await sleep(1500);
    }
    throw new Error("생성 시간이 너무 오래 걸립니다. 엔진 로그를 확인해주세요.");
  };

  const generate = async () => {
    if (mode === "image" && !file) {
      inputRef.current?.click();
      return;
    }
    if (mode === "text" && !prompt.trim()) {
      setNotice("만들고 싶은 오브젝트를 먼저 설명해주세요.");
      return;
    }
    if (engine !== "online") {
      setNotice("로컬 엔진을 연결하는 중입니다. 설치가 끝나면 자동으로 활성화됩니다.");
      return;
    }

    try {
      setModelUrl(null);
      setJob({ id: "", status: "submitting", progress: 2 });
      setNotice("RTX 5090에 생성 작업을 보내는 중입니다.");
      const body = new FormData();
      if (file) body.append("image", file);
      body.append("prompt", prompt.trim());
      body.append("quality", quality);
      body.append("remesh", String(remesh));
      body.append("texture", "true");
      const response = await fetch(`${API_BASE}/v1/generations`, {
        method: "POST",
        body,
      });
      if (!response.ok) {
        const message = await response.text();
        throw new Error(message || "생성 요청을 시작하지 못했습니다.");
      }
      const created = (await response.json()) as GenerationJob;
      setJob(created);
      await pollGeneration(created.id);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "알 수 없는 오류가 발생했습니다.";
      setJob((current) => ({ ...current, status: "failed", error: message }));
      setNotice(message);
    }
  };

  const progress = Math.max(0, Math.min(100, job.progress ?? 0));
  const isWorking = ["submitting", "queued", "running"].includes(job.status);
  const engineLabel =
    engine === "online"
      ? "ENGINE ONLINE"
      : engine === "checking"
        ? "CHECKING ENGINE"
        : "ENGINE OFFLINE";

  return (
    <main className="studio-shell">
      <header className="topbar">
        <a className="brand" href="#" aria-label="LocalForge 홈">
          <span className="brand-mark" aria-hidden="true">LF</span>
          <span>
            LOCALFORGE
            <small>3D LAB / 01</small>
          </span>
        </a>
        <div className="hardware-pill" title="로컬 추론 장치">
          <span className={`status-dot ${engine}`} aria-hidden="true" />
          <span>RTX 5090</span>
          <strong>32GB</strong>
        </div>
        <div className="top-actions">
          <span className="privacy-label">파일은 이 PC에서 처리됩니다</span>
          <button className="icon-button" type="button" aria-label="설정">···</button>
        </div>
      </header>

      <section className="workspace">
        <aside className="control-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">NEW ASSET</span>
              <h1>이미지를<br />입체로 만드세요.</h1>
            </div>
            <span className="step-number">01</span>
          </div>

          <div className="mode-tabs" role="tablist" aria-label="입력 방식">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "image"}
              className={mode === "image" ? "active" : ""}
              onClick={() => setMode("image")}
            >
              IMAGE → 3D
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "text"}
              className={mode === "text" ? "active" : ""}
              onClick={() => setMode("text")}
            >
              TEXT → 3D <span>BETA</span>
            </button>
          </div>

          {mode === "image" ? (
            <button
              className={`dropzone ${previewUrl ? "has-image" : ""}`}
              type="button"
              onClick={() => inputRef.current?.click()}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault();
                acceptFile(event.dataTransfer.files[0]);
              }}
            >
              <input
                ref={inputRef}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                onChange={(event) => acceptFile(event.target.files?.[0])}
                hidden
              />
              {previewUrl ? (
                <>
                  <img src={previewUrl} alt="선택한 3D 생성 원본" />
                  <span className="replace-image">이미지 교체</span>
                </>
              ) : (
                <>
                  <span className="upload-glyph" aria-hidden="true">+</span>
                  <strong>이미지를 놓거나 선택</strong>
                  <small>PNG · JPG · WEBP / 최대 20MB</small>
                </>
              )}
            </button>
          ) : (
            <label className="prompt-field">
              <span>오브젝트 설명</span>
              <textarea
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                placeholder="예: 오래된 황동 잠수 헬멧, 스튜디오 조명, 게임 에셋"
                maxLength={500}
              />
              <small>{prompt.length} / 500</small>
            </label>
          )}

          <div className="settings-block">
            <div className="setting-row">
              <span>품질<small>형상 해상도</small></span>
              <div className="segmented">
                <button
                  type="button"
                  className={quality === "draft" ? "active" : ""}
                  onClick={() => setQuality("draft")}
                >
                  DRAFT
                </button>
                <button
                  type="button"
                  className={quality === "studio" ? "active" : ""}
                  onClick={() => setQuality("studio")}
                >
                  STUDIO
                </button>
              </div>
            </div>
            <div className="setting-row">
              <span>출력<small>텍스처 포함</small></span>
              <strong className="output-format">GLB · PBR</strong>
            </div>
            <label className="setting-row toggle-row">
              <span>자동 리메시<small>가벼운 토폴로지</small></span>
              <input
                type="checkbox"
                checked={remesh}
                onChange={(event) => setRemesh(event.target.checked)}
              />
              <span className="toggle" aria-hidden="true" />
            </label>
          </div>

          <button
            type="button"
            className="generate-button"
            onClick={generate}
            disabled={isWorking}
          >
            <span>{isWorking ? "생성 중" : "3D 생성 시작"}</span>
            <span aria-hidden="true">{isWorking ? `${progress}%` : "↗"}</span>
          </button>
          <p className={`notice ${job.status === "failed" ? "error" : ""}`}>
            <span aria-hidden="true">i</span>{notice}
          </p>
        </aside>

        <section className="viewport" aria-label="3D 모델 미리보기">
          <div className="viewport-bar">
            <div>
              <span className={`status-dot ${engine}`} aria-hidden="true" />
              <strong>{engineLabel}</strong>
              <span>{engineName}</span>
            </div>
            <div><span>VIEW</span><strong>PERSPECTIVE</strong></div>
          </div>

          <div className="scene">
            <div className="scene-grid" aria-hidden="true" />
            {modelUrl ? (
              createElement(
                "model-viewer",
                {
                  src: modelUrl,
                  alt: "생성된 3D 모델",
                  "camera-controls": "",
                  "auto-rotate": "",
                  "shadow-intensity": "1.2",
                  exposure: "1.05",
                  style: { width: "100%", height: "100%" },
                } as never,
              )
            ) : (
              <div className={`seed-object ${isWorking ? "working" : ""}`}>
                <div className="seed-core" />
                <div className="orbit orbit-one" />
                <div className="orbit orbit-two" />
                <div className="orbit orbit-three" />
                <span>{isWorking ? `${progress}%` : "DROP / GENERATE"}</span>
              </div>
            )}
            <div className="axis-widget" aria-hidden="true">
              <i className="axis-x">X</i>
              <i className="axis-y">Y</i>
              <i className="axis-z">Z</i>
            </div>
            {isWorking && (
              <div className="progress-card">
                <div>
                  <span>GPU PROCESS</span>
                  <strong>{job.stage ?? "형상 생성 중"}</strong>
                </div>
                <span>{progress}%</span>
                <div className="progress-track">
                  <i style={{ width: `${progress}%` }} />
                </div>
              </div>
            )}
            {modelUrl && (
              <a className="download-button" href={modelUrl} download>
                GLB 다운로드 <span>↓</span>
              </a>
            )}
          </div>

          <footer className="pipeline-footer">
            {[
              ["01", "INPUT"],
              ["02", "GEOMETRY"],
              ["03", "PBR"],
              ["04", "EXPORT"],
            ].map(([number, label], index) => (
              <div
                key={label}
                className={
                  progress >= index * 25 || (index === 0 && file) ? "active" : ""
                }
              >
                <span>{number}</span><strong>{label}</strong>
              </div>
            ))}
          </footer>
        </section>
      </section>
    </main>
  );
}
