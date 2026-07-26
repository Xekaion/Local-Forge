"use client";

import { createElement, useEffect, useRef, useState } from "react";

type EngineState = "checking" | "online" | "offline";
type JobState =
  | "idle"
  | "submitting"
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

type IntegrityState = "idle" | "checking" | "verified" | "unavailable";

type GenerationJob = {
  id: string;
  status: JobState;
  progress?: number;
  stage?: string;
  model_url?: string;
  error?: string;
  output_sha256?: string;
  output_bytes?: number;
  manifest_sha256?: string;
  manifest_url?: string;
  input_sha256?: string;
  created_at?: string;
  updated_at?: string;
};

const API_BASE =
  process.env.NEXT_PUBLIC_LOCALFORGE_API ?? "http://127.0.0.1:8000";
const API_TOKEN = process.env.NEXT_PUBLIC_LOCALFORGE_API_TOKEN;
const API_HEADERS: HeadersInit = API_TOKEN
  ? { "X-LocalForge-Token": API_TOKEN }
  : {};
const MAX_FILE_SIZE = 20 * 1024 * 1024;
const sleep = (milliseconds: number) =>
  new Promise((resolve) => window.setTimeout(resolve, milliseconds));

const sha256Hex = async (contents: ArrayBuffer) => {
  if (!globalThis.crypto?.subtle) {
    throw new Error("이 브라우저에서는 SHA-256 검증을 사용할 수 없습니다.");
  }
  const digest = await globalThis.crypto.subtle.digest("SHA-256", contents);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
};

const resolveApiUrl = (url?: string) => {
  if (!url) return null;
  try {
    const apiUrl = new URL(API_BASE, window.location.href);
    const resolvedUrl = new URL(url, apiUrl);
    return resolvedUrl.origin === apiUrl.origin ? resolvedUrl.href : null;
  } catch {
    return null;
  }
};

const formatBytes = (bytes?: number) => {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) {
    return "확인 불가";
  }
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  );
  const value = bytes / 1024 ** index;
  return `${value.toFixed(index === 0 || value >= 10 ? 0 : 1)} ${units[index]}`;
};

const describeApiDetail = (detail: unknown): string | null => {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (
          item &&
          typeof item === "object" &&
          "msg" in item &&
          typeof item.msg === "string"
        ) {
          return item.msg;
        }
        return null;
      })
      .filter((message): message is string => Boolean(message));
    return messages.length ? messages.join(" · ") : null;
  }
  if (
    detail &&
    typeof detail === "object" &&
    "msg" in detail &&
    typeof detail.msg === "string"
  ) {
    return detail.msg;
  }
  return null;
};

const readApiError = async (response: Response, fallback: string) => {
  const raw = await response.text();
  if (!raw) return fallback;
  try {
    const payload = JSON.parse(raw) as {
      detail?: unknown;
      error?: unknown;
      message?: unknown;
    };
    return (
      describeApiDetail(payload.detail) ??
      describeApiDetail(payload.error) ??
      describeApiDetail(payload.message) ??
      fallback
    );
  } catch {
    return raw;
  }
};

export default function Home() {
  const inputRef = useRef<HTMLInputElement>(null);
  const pollAbortRef = useRef<AbortController | null>(null);
  const modelObjectUrlRef = useRef<string | null>(null);
  const manifestObjectUrlRef = useRef<string | null>(null);
  const idempotencyKeyRef = useRef<string | null>(null);
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
  const [manifestDownloadUrl, setManifestDownloadUrl] = useState<string | null>(
    null,
  );
  const [integrityState, setIntegrityState] =
    useState<IntegrityState>("idle");
  const [isCancelling, setIsCancelling] = useState(false);
  const [copied, setCopied] = useState(false);
  const [notice, setNotice] = useState(
    "정면이 잘 보이고 배경이 단순한 이미지를 넣어주세요.",
  );
  const isWorking = ["submitting", "queued", "running"].includes(job.status);

  useEffect(() => {
    void import("@google/model-viewer");
  }, []);

  useEffect(
    () => () => {
      pollAbortRef.current?.abort();
      if (modelObjectUrlRef.current) {
        URL.revokeObjectURL(modelObjectUrlRef.current);
      }
      if (manifestObjectUrlRef.current) {
        URL.revokeObjectURL(manifestObjectUrlRef.current);
      }
    },
    [],
  );

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
    fetch(`${API_BASE}/health`, {
      headers: API_HEADERS,
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(
            await readApiError(response, "engine unavailable"),
          );
        }
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

  const clearArtifacts = () => {
    if (modelObjectUrlRef.current) {
      URL.revokeObjectURL(modelObjectUrlRef.current);
      modelObjectUrlRef.current = null;
    }
    if (manifestObjectUrlRef.current) {
      URL.revokeObjectURL(manifestObjectUrlRef.current);
      manifestObjectUrlRef.current = null;
    }
    setModelUrl(null);
    setManifestDownloadUrl(null);
    setIntegrityState("idle");
  };

  const acceptFile = (nextFile?: File) => {
    if (isWorking) {
      setNotice("현재 생성 작업을 취소하거나 완료한 뒤 입력을 변경해 주세요.");
      return;
    }
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
    idempotencyKeyRef.current = null;
    clearArtifacts();
    setCopied(false);
    setJob({ id: "", status: "idle", progress: 0 });
    setNotice("입력 준비 완료. 로컬 GPU로 생성할 수 있습니다.");
  };

  const loadVerifiedArtifacts = async (
    completedJob: GenerationJob,
    signal: AbortSignal,
  ) => {
    const outputUrl = resolveApiUrl(completedJob.model_url);
    const manifestUrl = resolveApiUrl(completedJob.manifest_url);
    if (
      !outputUrl ||
      !manifestUrl ||
      !completedJob.output_sha256 ||
      !completedJob.manifest_sha256
    ) {
      throw new Error(
        "완료 응답에 출력 해시 또는 무결성 매니페스트가 없습니다.",
      );
    }

    setIntegrityState("checking");
    setNotice("GLB와 매니페스트를 내려받아 브라우저에서 다시 검증하는 중입니다.");

    const outputResponse = await fetch(outputUrl, {
      headers: API_HEADERS,
      signal,
    });
    if (!outputResponse.ok) {
      throw new Error(
        await readApiError(outputResponse, "검증할 GLB를 내려받지 못했습니다."),
      );
    }
    const outputBuffer = await outputResponse.arrayBuffer();
    const outputHash = await sha256Hex(outputBuffer);
    const outputHeaderHash = outputResponse.headers.get("X-Checksum-SHA256");
    if (
      outputHash !== completedJob.output_sha256 ||
      (outputHeaderHash && outputHeaderHash !== outputHash)
    ) {
      throw new Error("GLB SHA-256이 서버 기록과 일치하지 않습니다.");
    }
    if (
      typeof completedJob.output_bytes !== "number" ||
      outputBuffer.byteLength !== completedJob.output_bytes
    ) {
      throw new Error("GLB 파일 크기가 서버 기록과 일치하지 않습니다.");
    }

    const manifestResponse = await fetch(manifestUrl, {
      headers: API_HEADERS,
      signal,
    });
    if (!manifestResponse.ok) {
      throw new Error(
        await readApiError(
          manifestResponse,
          "무결성 매니페스트를 내려받지 못했습니다.",
        ),
      );
    }
    const manifestBuffer = await manifestResponse.arrayBuffer();
    const manifestHash = await sha256Hex(manifestBuffer);
    const manifestHeaderHash =
      manifestResponse.headers.get("X-Checksum-SHA256");
    if (
      manifestHash !== completedJob.manifest_sha256 ||
      (manifestHeaderHash && manifestHeaderHash !== manifestHash)
    ) {
      throw new Error("매니페스트 SHA-256 응답 헤더가 일치하지 않습니다.");
    }

    let manifest: {
      job_id?: string;
      output?: { bytes?: number; sha256?: string };
    };
    try {
      manifest = JSON.parse(
        new TextDecoder().decode(manifestBuffer),
      ) as typeof manifest;
    } catch {
      throw new Error("무결성 매니페스트가 올바른 JSON이 아닙니다.");
    }
    if (
      manifest.job_id !== completedJob.id ||
      manifest.output?.sha256 !== outputHash ||
      manifest.output?.bytes !== outputBuffer.byteLength
    ) {
      throw new Error("매니페스트의 작업 ID 또는 출력 기록이 일치하지 않습니다.");
    }

    if (signal.aborted) return;
    clearArtifacts();
    const outputObjectUrl = URL.createObjectURL(
      new Blob([outputBuffer], {
        type: outputResponse.headers.get("content-type") ?? "model/gltf-binary",
      }),
    );
    const manifestObjectUrl = URL.createObjectURL(
      new Blob([manifestBuffer], { type: "application/json" }),
    );
    modelObjectUrlRef.current = outputObjectUrl;
    manifestObjectUrlRef.current = manifestObjectUrl;
    setModelUrl(outputObjectUrl);
    setManifestDownloadUrl(manifestObjectUrl);
    setIntegrityState("verified");
  };

  const pollGeneration = async (jobId: string, signal: AbortSignal) => {
    let consecutiveFailures = 0;
    while (!signal.aborted) {
      let response: Response;
      try {
        response = await fetch(`${API_BASE}/v1/generations/${jobId}`, {
          headers: API_HEADERS,
          signal,
        });
      } catch {
        if (signal.aborted) return;
        consecutiveFailures += 1;
        const retrySeconds = Math.min(
          15,
          1.5 * 2 ** Math.min(consecutiveFailures - 1, 4),
        );
        setNotice(
          `엔진 연결이 끊겼습니다. 작업은 유지하며 ${retrySeconds}초 뒤 다시 확인합니다.`,
        );
        await sleep(retrySeconds * 1000);
        continue;
      }
      if (!response.ok) {
        if (
          response.status === 408 ||
          response.status === 429 ||
          response.status >= 500
        ) {
          consecutiveFailures += 1;
          const retrySeconds = Math.min(
            15,
            1.5 * 2 ** Math.min(consecutiveFailures - 1, 4),
          );
          setNotice(
            `엔진이 일시적으로 응답하지 않습니다. 작업은 유지하며 ${retrySeconds}초 뒤 다시 확인합니다.`,
          );
          await response.text();
          await sleep(retrySeconds * 1000);
          continue;
        }
        throw new Error(
          await readApiError(response, "작업 상태를 확인하지 못했습니다."),
        );
      }
      consecutiveFailures = 0;
      const current = (await response.json()) as GenerationJob;
      setJob(current);
      if (current.status === "succeeded") {
        await loadVerifiedArtifacts(current, signal);
        idempotencyKeyRef.current = null;
        setNotice("완료됐습니다. GLB와 매니페스트의 SHA-256을 재검증했습니다.");
        return;
      }
      if (current.status === "failed") {
        idempotencyKeyRef.current = null;
        throw new Error(current.error ?? "3D 생성에 실패했습니다.");
      }
      if (current.status === "cancelled") {
        idempotencyKeyRef.current = null;
        setNotice("생성 작업이 취소됐습니다. 입력을 바꿔 다시 시작할 수 있습니다.");
        return;
      }
      await sleep(1500);
    }
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
      clearArtifacts();
      setCopied(false);
      setJob({ id: "", status: "submitting", progress: 2 });
      setNotice("RTX 5090에 생성 작업을 보내는 중입니다.");
      const body = new FormData();
      if (mode === "image" && file) body.append("image", file);
      body.append("prompt", mode === "text" ? prompt.trim() : "");
      body.append("quality", quality);
      body.append("remesh", String(remesh));
      body.append("texture", "true");
      const idempotencyKey =
        idempotencyKeyRef.current ?? globalThis.crypto.randomUUID();
      idempotencyKeyRef.current = idempotencyKey;
      const response = await fetch(`${API_BASE}/v1/generations`, {
        method: "POST",
        headers: {
          ...API_HEADERS,
          "Idempotency-Key": idempotencyKey,
        },
        body,
      });
      if (!response.ok) {
        throw new Error(
          await readApiError(response, "생성 요청을 시작하지 못했습니다."),
        );
      }
      const created = (await response.json()) as GenerationJob;
      setJob(created);
      const controller = new AbortController();
      pollAbortRef.current = controller;
      await pollGeneration(created.id, controller.signal);
      if (pollAbortRef.current === controller) {
        pollAbortRef.current = null;
      }
    } catch (error) {
      pollAbortRef.current = null;
      if (error instanceof Error && error.name === "AbortError") return;
      const message =
        error instanceof Error ? error.message : "알 수 없는 오류가 발생했습니다.";
      setJob((current) => ({ ...current, status: "failed", error: message }));
      setNotice(message);
    }
  };

  const cancelGeneration = async () => {
    if (!job.id || !isWorking || isCancelling) return;
    setIsCancelling(true);
    setNotice("생성 작업을 안전하게 중단하는 중입니다.");
    try {
      const response = await fetch(`${API_BASE}/v1/generations/${job.id}`, {
        method: "DELETE",
        headers: API_HEADERS,
      });
      if (!response.ok) {
        throw new Error(
          await readApiError(response, "생성 작업을 취소하지 못했습니다."),
        );
      }

      const raw = await response.text();
      let cancelledJob: GenerationJob | null = null;
      if (raw) {
        try {
          cancelledJob = JSON.parse(raw) as GenerationJob;
        } catch {
          cancelledJob = null;
        }
      }

      const nextJob: GenerationJob = {
        ...job,
        ...(cancelledJob ?? {}),
        id: cancelledJob?.id ?? job.id,
      };
      setJob(nextJob);
      if (nextJob.status === "cancelled") {
        idempotencyKeyRef.current = null;
        pollAbortRef.current?.abort();
        pollAbortRef.current = null;
        clearArtifacts();
        setNotice(
          "생성 작업이 취소됐습니다. 입력을 바꿔 다시 시작할 수 있습니다.",
        );
      } else {
        setNotice(
          "취소 요청을 접수했습니다. 현재 GPU 단계가 안전하게 끝나면 중단됩니다.",
        );
      }
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "생성 작업을 취소하지 못했습니다.";
      setNotice(message);
    } finally {
      setIsCancelling(false);
    }
  };

  const copyOutputHash = async () => {
    if (!job.output_sha256) return;
    try {
      await navigator.clipboard.writeText(job.output_sha256);
      setCopied(true);
      setNotice("출력 SHA-256 해시를 클립보드에 복사했습니다.");
    } catch {
      setNotice("해시를 복사할 수 없습니다. 보안 브라우저 설정을 확인해주세요.");
    }
  };

  const progress = Math.max(0, Math.min(100, job.progress ?? 0));
  const integrityVerified =
    job.status === "succeeded" && integrityState === "verified";
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
              disabled={isWorking}
              onClick={() => {
                idempotencyKeyRef.current = null;
                setMode("image");
              }}
            >
              IMAGE → 3D
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "text"}
              className={mode === "text" ? "active" : ""}
              disabled={isWorking}
              onClick={() => {
                idempotencyKeyRef.current = null;
                setMode("text");
              }}
            >
              TEXT → 3D <span>BETA</span>
            </button>
          </div>

          {mode === "image" ? (
            <button
              className={`dropzone ${previewUrl ? "has-image" : ""}`}
              type="button"
              disabled={isWorking}
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
                disabled={isWorking}
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
                onChange={(event) => {
                  idempotencyKeyRef.current = null;
                  setPrompt(event.target.value);
                }}
                placeholder="예: 오래된 황동 잠수 헬멧, 스튜디오 조명, 게임 에셋"
                maxLength={500}
                disabled={isWorking}
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
                  disabled={isWorking}
                  onClick={() => {
                    idempotencyKeyRef.current = null;
                    setQuality("draft");
                  }}
                >
                  DRAFT
                </button>
                <button
                  type="button"
                  className={quality === "studio" ? "active" : ""}
                  disabled={isWorking}
                  onClick={() => {
                    idempotencyKeyRef.current = null;
                    setQuality("studio");
                  }}
                >
                  STUDIO
                </button>
              </div>
            </div>
            <div className="setting-row">
              <span>출력<small>텍스처 포함</small></span>
              <strong className="output-format">GLB · PBR</strong>
            </div>
            <div className="setting-row">
              <span>검증<small>출력 파일 무결성</small></span>
              <strong className="output-format">SHA-256 · MANIFEST</strong>
            </div>
            <label className="setting-row toggle-row">
              <span>자동 리메시<small>가벼운 토폴로지</small></span>
              <input
                type="checkbox"
                checked={remesh}
                disabled={isWorking}
                onChange={(event) => {
                  idempotencyKeyRef.current = null;
                  setRemesh(event.target.checked);
                }}
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
          {isWorking && (
            <button
              type="button"
              className="cancel-button"
              onClick={cancelGeneration}
              disabled={
                !job.id || isCancelling || job.stage === "취소 요청됨"
              }
              aria-label="현재 3D 생성 작업 취소"
            >
              {isCancelling
                ? "취소 요청 중"
                : job.stage === "취소 요청됨"
                  ? "안전 중단 대기"
                : job.id
                  ? "생성 취소"
                  : "작업 등록 중"}
            </button>
          )}
          <p
            className={`notice ${job.status === "failed" ? "error" : ""} ${
              job.status === "cancelled" ? "cancelled" : ""
            }`}
            role={job.status === "failed" ? "alert" : "status"}
            aria-live={job.status === "failed" ? "assertive" : "polite"}
          >
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
              <div
                className={`seed-object ${
                  isWorking || integrityState === "checking" ? "working" : ""
                }`}
              >
                <div className="seed-core" />
                <div className="orbit orbit-one" />
                <div className="orbit orbit-two" />
                <div className="orbit orbit-three" />
                <span>
                  {integrityState === "checking"
                    ? "VERIFYING SHA-256"
                    : isWorking
                      ? `${progress}%`
                      : "DROP / GENERATE"}
                </span>
              </div>
            )}
            <div className="axis-widget" aria-hidden="true">
              <i className="axis-x">X</i>
              <i className="axis-y">Y</i>
              <i className="axis-z">Z</i>
            </div>
            {isWorking && (
              <div
                className="progress-card"
                role="progressbar"
                aria-label="3D 생성 진행률"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={progress}
                aria-valuetext={`${job.stage ?? "형상 생성 중"} ${progress}%`}
              >
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
            {modelUrl && job.status === "succeeded" && (
              <aside className="result-card" aria-label="생성 결과 무결성">
                <div className="result-card-heading">
                  <span
                    className={`verification-badge ${
                      integrityVerified ? "verified" : "unavailable"
                    }`}
                  >
                    <i aria-hidden="true">
                      {integrityVerified ? "✓" : "!"}
                    </i>
                    {integrityVerified
                      ? "무결성 검증 완료"
                      : "검증 메타데이터 없음"}
                  </span>
                  <strong>{formatBytes(job.output_bytes)}</strong>
                </div>

                {job.output_sha256 && (
                  <div className="hash-row">
                    <span>OUTPUT SHA-256</span>
                    <code title={job.output_sha256}>{job.output_sha256}</code>
                    <button type="button" onClick={copyOutputHash}>
                      {copied ? "복사됨" : "해시 복사"}
                    </button>
                  </div>
                )}

                <div className="result-actions">
                  <a href={modelUrl} download={`localforge-${job.id}.glb`}>
                    GLB 다운로드 <span aria-hidden="true">↓</span>
                  </a>
                  {manifestDownloadUrl && (
                    <a
                      href={manifestDownloadUrl}
                      download={`${job.id}-manifest.json`}
                    >
                      매니페스트 JSON <span aria-hidden="true">↓</span>
                    </a>
                  )}
                </div>
              </aside>
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
