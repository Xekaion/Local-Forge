# LocalForge 인수인계

이 문서는 다른 ChatGPT/Codex 계정과 RTX 5090 32GB가 장착된 Windows PC에서
LocalForge 개발을 바로 이어가기 위한 기준 문서다.

- 저장소: <https://github.com/Xekaion/Local-Forge>
- 인수인계 기준일: 2026-07-26
- 기존 MVP 기준 커밋: `5f2f7bf` (`Build LocalForge image-to-3D MVP`)
- 목표: 입력 이미지 한 장을 로컬 TRELLIS.2로 처리해 PBR GLB로 미리 보고
  다운로드하는 로컬 우선 3D 생성기

## 1. 현재 상태

### 완료된 범위

- 이미지 드래그 앤 드롭/선택, 로컬 미리 보기 및 파일 검증
  - PNG, JPG, WEBP
  - 최대 20MB
- 로컬 엔진 상태 확인과 비동기 작업 생성/폴링 UI
- Draft/Studio 품질, 리메시 옵션 및 생성 진행률 표시
- `@google/model-viewer` 기반 GLB 회전 미리 보기와 다운로드
- GPU 없이 전체 흐름을 확인하는 mock FastAPI 백엔드
- TRELLIS.2 공개 예제를 따르는 실제 백엔드 어댑터 골격
- 로컬 빌드 및 린트 확인
- 현재 LocalForge SSR 기준 자동 렌더 테스트 2건 통과
- mock 작업 생성 → GLB 출력 → GLB 재로드까지의 종단 간 확인
- 기존 ChatGPT 계정의 OpenAI Sites 프로젝트에 비공개 배포

### 아직 완료되지 않은 범위

- RTX 5090 PC에서 CUDA/TRELLIS.2 확장 모듈 설치 및 실제 추론 검증
- `microsoft/TRELLIS.2-4B` 체크포인트를 사용한 실제 이미지 → GLB 성공 기록
- 작동한 TRELLIS.2 커밋, CUDA/PyTorch/확장 모듈 버전 고정
- VRAM 사용량, 생성 시간, Draft/Studio 안정성 측정
- 텍스트 전용 3D 생성
  - UI 탭은 있으나 이미지 없이 프롬프트만 보내면 엔진이 HTTP 501을 반환한다.
- 다중 생성 요청의 안전한 직렬 처리
  - 엔진 프로세스는 하나만 실행해야 한다. 현재 작업 스레드가 여러 개 생길 수
    있으므로 다중 사용자 사용 전에는 명시적인 GPU 큐/세마포어를 추가해야 한다.
- 인증, 사용자별 작업 보관, 오래된 입력/출력 파일 자동 정리

## 2. 아키텍처

```text
브라우저 UI
  └─ Next.js 16 + React 19 + vinext
       ├─ GET  /health
       ├─ POST /v1/generations
       └─ GET  /v1/generations/{id}
              │
              ▼
       FastAPI 로컬 엔진 :8000
       ├─ mock      → trimesh로 테스트 GLB 생성
       └─ trellis2  → TRELLIS.2-4B → o_voxel → PBR GLB
              │
              └─ GET /outputs/{id}.glb
```

핵심 파일:

| 경로 | 역할 |
| --- | --- |
| `app/page.tsx` | 업로드, 엔진 상태 확인, 작업 폴링, GLB 미리 보기/다운로드 |
| `app/globals.css` | 전체 제품 UI와 반응형 스타일 |
| `app/layout.tsx` | 한국어 메타데이터와 OG 이미지 URL 구성 |
| `engine/app.py` | FastAPI API, 메모리 작업 상태, mock/TRELLIS 분기, 정적 GLB 제공 |
| `engine/trellis_backend.py` | TRELLIS.2 로드, CUDA 확인, 품질별 GLB 후처리 |
| `engine/requirements.txt` | 로컬 브리지와 mock 실행 의존성 |
| `engine/start-engine.ps1` | Windows mock 엔진 실행 보조 스크립트 |
| `docs/RTX5090-TRELLIS2.md` | RTX 5090 + WSL2 + CUDA 13 설치 절차와 근거 링크 |
| `.env.example` | 브라우저가 호출할 로컬 API 주소 예시 |
| `.openai/hosting.json` | 기존 ChatGPT 계정의 Sites 프로젝트 연결 정보 |

엔진의 작업 상태는 메모리에만 있고 입력과 출력은 각각
`engine/runtime/inputs`, `engine/runtime/outputs`에 저장된다. 엔진을 다시
시작하면 기존 작업 상태는 복원되지 않는다. 이 디렉터리는 Git에서 제외되어 있다.

## 3. 새 RTX 5090 PC 준비

권장 기준:

- Windows 11과 최신 NVIDIA Windows 드라이버
- WSL2 + Ubuntu 24.04
- RTX 5090 32GB
- 시스템 RAM 64GB 권장
- 여유 SSD 100GB 이상 권장
- Git
- Windows 쪽 Node.js `>=22.13.0`
- WSL 쪽 Conda와 Python 3.10

중요 원칙:

- TRELLIS.2 저장소와 빌드 산출물은 `/mnt/c`가 아니라
  `~/src/TRELLIS.2`처럼 WSL 리눅스 파일시스템에 둔다.
- WSL 안에 NVIDIA 디스플레이 드라이버를 별도로 설치하지 않는다. Windows 호스트
  드라이버를 사용하고 WSL에는 필요한 CUDA toolkit만 설치한다.
- 설치를 시작하기 전에 WSL에서 `nvidia-smi`가 RTX 5090을 표시해야 한다.
- TRELLIS 엔진은 `uvicorn --workers` 옵션을 늘리지 말고 단일 프로세스로 실행한다.

실제 CUDA/PyTorch/확장 모듈 설치 명령은
[RTX 5090 + TRELLIS.2 설정 문서](docs/RTX5090-TRELLIS2.md)를 그대로 따른다.
공식 스크립트의 오래된 PyTorch/CUDA 고정값을 그대로 사용하지 않는 이유도 그
문서에 정리되어 있다.

## 4. Clone 후 기본 검증

비공개 저장소라면 새 PC에서 GitHub에 먼저 로그인한다. 개인 액세스 토큰을 clone
URL이나 문서에 넣지 않는다.

```powershell
git clone https://github.com/Xekaion/Local-Forge.git
Set-Location Local-Forge
git status
git log -1 --oneline

node --version
npm --version
npm ci
npm run build
npm run lint
npm test
```

린트에는 현재 `app/page.tsx`의 로컬 이미지 미리 보기에 대한 Next.js `<img>`
권고가 한 건 나올 수 있다. 빌드 실패와는 별개다. `npm test`는 배포 빌드를
다시 실행한 뒤 현재 LocalForge SSR 화면과 스타터 UI 제거 상태를 검사한다.

작업을 시작하기 전에 새 브랜치를 만드는 것을 권장한다.

```powershell
git switch -c feat/trellis2-rtx5090
```

## 5. GPU 없이 mock 흐름 확인

첫 번째 PowerShell:

```powershell
python -m venv engine\.venv
engine\.venv\Scripts\python.exe -m pip install --upgrade pip
engine\.venv\Scripts\python.exe -m pip install -r engine\requirements.txt

$env:LOCALFORGE_BACKEND = "mock"
engine\.venv\Scripts\python.exe -m uvicorn app:app `
  --app-dir engine --host 127.0.0.1 --port 8000
```

별도 PowerShell에서 상태를 확인한다.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

응답의 `status`는 `online`, `backend`는 `mock`이어야 한다.

두 번째 PowerShell:

```powershell
npm run dev
```

웹 서버가 출력한 로컬 주소를 열고 다음을 확인한다.

1. 엔진 표시가 `ENGINE ONLINE`으로 바뀐다.
2. 20MB 이하 PNG/JPG/WEBP를 선택한다.
3. `3D 생성 시작`을 누르면 진행률이 증가한다.
4. 완료 후 3D 미리 보기가 회전하고 GLB가 다운로드된다.

mock 결과는 AI 생성물이 아니라 제품의 업로드, 작업 상태, 미리 보기, 다운로드
경로만 검증하는 테스트 메시다.

## 6. 실제 TRELLIS.2 연결

먼저 [docs/RTX5090-TRELLIS2.md](docs/RTX5090-TRELLIS2.md)의 순서로 다음을
완료한다.

1. WSL Ubuntu 24.04에서 RTX 5090과 compute capability `(12, 0)` 확인
2. Python 3.10 Conda 환경 생성
3. CUDA 13 계열 PyTorch 설치
4. `TORCH_CUDA_ARCH_LIST=12.0`으로 TRELLIS.2 확장 모듈을 하나씩 빌드
5. 필요한 경우 해당 문서가 연결한 CuMesh/RTX 5090 이슈의 패치 적용
6. PyTorch에서 CUDA, GPU 이름, capability 재확인

그 후 WSL의 TRELLIS Conda 환경에서 LocalForge 엔진을 실행한다.

```bash
cd /mnt/c/path/to/Local-Forge
pip install -r engine/requirements.txt

export LOCALFORGE_BACKEND=trellis2
export TRELLIS2_ROOT=~/src/TRELLIS.2
python -m uvicorn app:app --app-dir engine \
  --host 127.0.0.1 --port 8000
```

Windows에서 `npm run dev`를 실행한다. WSL의 포트가 Windows
`127.0.0.1:8000`으로 전달되지 않는 환경이라면 WSL 네트워크 주소와 Uvicorn bind
주소를 조정하고 `NEXT_PUBLIC_LOCALFORGE_API`도 함께 변경한다. LAN이나 외부
도메인에서 접근시키려면 `engine/app.py`의 로컬 전용 CORS 정책도 별도로
검토해야 한다.

첫 실제 테스트는 Draft 품질, 요청 한 건으로 시작한다. 최초 로드 시
`microsoft/TRELLIS.2-4B` 체크포인트가 내려받아질 수 있으며 Hugging Face 접근
권한이 필요할 수 있다. 토큰이 필요하면 로컬 환경변수나 Hugging Face CLI
로그인을 사용하고 Git에는 절대 기록하지 않는다.

## 7. 환경변수와 포트

| 이름 | 위치 | 값/의미 |
| --- | --- | --- |
| `NEXT_PUBLIC_LOCALFORGE_API` | 웹 앱 | 기본값 `http://127.0.0.1:8000` |
| `LOCALFORGE_BACKEND` | 엔진 | `mock` 또는 `trellis2`; 미지정 시 `mock` |
| `TRELLIS2_ROOT` | TRELLIS 엔진 | WSL의 TRELLIS.2 checkout 절대 경로 |
| `CUDA_HOME` | WSL 빌드 | 설정 문서 기준 CUDA toolkit 경로 |
| `TORCH_CUDA_ARCH_LIST` | WSL 빌드 | RTX 5090용 `12.0` |
| `MAX_JOBS` | WSL 빌드 | 컴파일 병렬도; 문서에서는 `3` 권장 |
| `ATTN_BACKEND` | TRELLIS 런타임 | 현재 권장값 `sdpa` |
| `SPARSE_ATTN_BACKEND` | TRELLIS 런타임 | 현재 권장값 `xformers` |

- 엔진 고정 포트: `127.0.0.1:8000`
- 웹 개발 서버: `npm run dev`가 출력한 주소 사용
- `.env.example`을 참고해 필요할 때만 `.env.local`을 만든다.

## 8. 계정 종속 항목과 보안

`.openai/hosting.json`의 `project_id`는 기존 ChatGPT/OpenAI 계정에서 만든
Sites 프로젝트에 연결되어 있다. 이 값은 모델 비밀키는 아니지만 다른 계정의
소유권을 자동으로 넘겨주지 않는다.

- 로컬 실행만 할 때는 해당 파일을 사용할 필요가 없다.
- 새 ChatGPT 계정에서 다시 배포하기 전에 기존 프로젝트 접근 가능 여부를 먼저
  확인한다.
- 접근할 수 없다면 기존 `project_id`를 재사용하지 말고 새 계정에서 Sites
  프로젝트를 만든 뒤 `.openai/hosting.json`을 그 프로젝트 정보로 교체한다.
- 새 Sites 배포 URL과 프로젝트 ID는 새 계정에서 별도로 관리한다.

GitHub 보안 규칙:

- 인수인계 시점의 `Xekaion/Local-Forge` 저장소는 공개 상태다. 커밋한 내용은
  누구나 볼 수 있다고 가정한다.
- `.env.local`, Hugging Face 토큰, GitHub 토큰, API 키를 커밋하지 않는다.
- 토큰을 포함한 원격 URL을 Git 설정에 남기지 않는다.
- `node_modules`, `engine/.venv`, `engine/runtime`, 모델 체크포인트와 생성 GLB를
  커밋하지 않는다. 현재 `.gitignore`가 이 중 프로젝트 런타임 경로를 제외한다.
- 큰 모델 파일은 이 저장소에 넣지 않는다. TRELLIS.2와 체크포인트는 외부
  checkout/cache로 유지한다.
- push 전에는 반드시 `git status`와 `git diff --cached`로 비밀값과 대용량
  바이너리가 없는지 확인한다.

## 9. 다음 ChatGPT/Codex에 전달할 첫 프롬프트

아래 문장을 새 PC의 LocalForge 저장소를 연 ChatGPT/Codex에 그대로 전달한다.

```text
이 저장소의 README.md, HANDOFF.md, engine/README.md,
docs/RTX5090-TRELLIS2.md를 먼저 전부 읽어줘. 기존 MVP 코드는 보존하고
git status부터 확인해. 이 PC에는 RTX 5090 32GB가 있으니 WSL2의 nvidia-smi,
Windows/WSL/드라이버/디스크/RAM/Node/Python 상태를 읽기 전용으로 점검한 뒤
mock 종단 간 흐름을 먼저 재검증해줘. 이후 docs/RTX5090-TRELLIS2.md를 기준으로
TRELLIS.2를 설치하고 LOCALFORGE_BACKEND=trellis2로 실제 이미지 한 장을 GLB로
생성해 웹 미리 보기와 다운로드까지 검증해줘. 한 번에 GPU 작업 하나만 실행하고,
작동한 TRELLIS.2 커밋과 CUDA/PyTorch/확장 모듈 버전, 실행 명령, VRAM/시간,
적용한 패치를 문서화해. .openai/hosting.json은 이전 계정의 Sites 프로젝트이므로
내 확인 없이 재사용하거나 배포하지 말고, 토큰·키·체크포인트는 절대 커밋하지 마.
검증이 끝나면 변경을 새 브랜치에 커밋하되 push 전 diff를 보여줘.
```

## 10. 다음 단계와 완료 기준

우선순위:

1. 새 PC 하드웨어/WSL 상태 점검
2. 현재 소스의 build/lint/mock 스모크 테스트
3. RTX 5090용 TRELLIS.2 의존성 빌드
4. 실제 이미지 한 장의 Draft GLB 생성
5. Studio 품질과 VRAM/시간 측정
6. 생성 요청을 직렬화하는 GPU 큐 또는 세마포어 추가
7. 작동 버전과 장애 해결 내용을 README/설정 문서에 고정
8. 필요하면 새 계정의 Sites 프로젝트로 별도 배포

실제 연동 완료 기준:

- WSL에서 `nvidia-smi`와 PyTorch가 RTX 5090을 정상 인식한다.
- PyTorch CUDA capability가 `(12, 0)`으로 확인된다.
- 필요한 TRELLIS.2 CUDA 확장 모듈이 `sm_120` 대상으로 로드된다.
- `GET /health`가 `backend: "trellis2"`와 `model: "TRELLIS.2-4B"`를 반환한다.
- 실제 이미지 업로드가 실패 없이 `succeeded` 상태에 도달한다.
- 생성 GLB를 웹 `model-viewer`에서 열고 다운로드할 수 있다.
- GLB 파일을 `trimesh`나 Blender에서 다시 열어 유효성을 확인한다.
- Draft 기준 생성 시간, 최고 VRAM 사용량, 결과 파일 크기를 기록한다.
- 사용한 upstream 커밋과 정확한 환경 버전 및 패치를 저장소 문서에 남긴다.
- 토큰, 로컬 환경파일, 체크포인트, 입력 이미지, 생성 결과가 Git 추적 대상에
  포함되지 않는다.
