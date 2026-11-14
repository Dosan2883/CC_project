# AI 협업 메신저 데모

Notion 과제 명세의 우선 기반 기능을 구현했습니다. 계정·방·메시지·설정·미디어 작업을 영속 저장하고, AWS 연결은 환경변수로 선택합니다. 실제 AWS 리소스 생성·배포는 실행하지 않았습니다.

## 실행

프로젝트 루트에서 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

브라우저에서 <http://127.0.0.1:8000>에 접속합니다. 기본은 로컬 SQLite와 파일 저장소이며 워커도 함께 실행됩니다. `data/messenger.sqlite3`에 계정·방·대화·설정·작업·로컬 서명 키를 저장하므로 서버 재시작 후에도 유지됩니다. 기존 `data/original/`, `data/extracted/` 파일은 이동하거나 삭제하지 않았습니다. 과거 메모리에만 있던 데이터는 복원할 수 없습니다.

현재 포함된 Python 3.14.0rc2는 최신 Pydantic과 typing 호출이 맞지 않아 시작하지 못했습니다. 이 RC 버전에만 Pydantic 2.12.0을 설치하도록 `requirements.txt`에 조건을 추가했습니다. Python 표준 라이브러리와 애플리케이션 런타임에 임시 패치를 넣지 않습니다. 새 배포 환경은 정식 Python 3.11 이상으로 구성합니다.

### Lambda 연결 (번역·요약)

프로젝트 폴더에서 PowerShell로 **한 줄씩** 실행합니다. 값을 입력하고 Enter를 누른 뒤에 다음 줄을 실행하세요. 여러 줄을 한꺼번에 붙여 넣으면 명령어 글자가 값으로 저장됩니다. URL과 비밀 값은 채팅이나 저장소에 붙여 넣지 않습니다.

1. 함수 URL 입력 (`https://`로 시작해야 합니다)

```powershell
$url = Read-Host "함수 URL"
```

2. 비밀 값 입력 (입력해도 화면에 보이지 않는 것이 정상입니다)

```powershell
$sec = Read-Host "비밀 값" -AsSecureString
```

3. `.env` 생성 (기존 `.env`는 덮어씁니다. 다른 설정이 있으면 `TRANSLATE_URL`, `TRANSLATE_SECRET` 두 줄만 직접 고치세요)

```powershell
$secret = [System.Net.NetworkCredential]::new("", $sec).Password; [System.IO.File]::WriteAllText("$PWD\.env", "TRANSLATE_URL=$url`nTRANSLATE_SECRET=$secret`n", (New-Object System.Text.UTF8Encoding $false)); "만들어짐"
```

4. 연결 시험 (`성공: Hello...`가 나오면 정상)

```powershell
.\.venv\Scripts\python.exe -c "
from dotenv import dotenv_values
from app.ai.client import LambdaTranslator, TranslationUnavailable
c = dotenv_values('.env')
t = LambdaTranslator(c['TRANSLATE_URL'], c['TRANSLATE_SECRET'])
try:
    print('성공:', t.translate('안녕하세요', 'English'))
except TranslationUnavailable as e:
    print('실패:', e, '| 원인:', repr(e.__cause__))
"
```

5. 서버 실행 (`.env`를 고친 뒤에는 서버를 껐다 켜야 값을 읽습니다)

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

브라우저에서 <http://127.0.0.1:8000>에 접속합니다. 실패 메시지 `response is missing completion metadata`는 Lambda 응답이 위 계약(`finishReason`, `truncated`, `modelVersion`)에 맞지 않을 때 나옵니다.

### EC2 서버 운영 (Learner Lab)

`키파일경로`, `새IP`는 본인 값으로 바꿉니다. IP와 키 파일, `.env` 값은 채팅이나 저장소에 올리지 않습니다.

**서버를 다시 켤 때** (Lab 세션을 새로 시작한 뒤)

1. Lab에서 **Start Lab**을 누르고 EC2 콘솔에서 인스턴스가 실행 중인지 확인합니다. 중지했다가 켜면 **공개 IP가 바뀌므로** 새 IP를 확인합니다.
2. 내 PC의 PowerShell에서 접속합니다.

```powershell
ssh -i "키파일경로\키이름.pem" ubuntu@새IP
```

3. 접속한 서버 안에서 앱을 시작합니다. 설치한 라이브러리, 모델, `.env`, `data`는 서버 디스크에 남아 있어 다시 설정하지 않습니다. 앱은 자동으로 켜지지 않아 이 명령이 필요합니다.

```bash
pkill -f "uvicorn main:app"; cd ~/messenger && nohup .venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000 > app.log 2>&1 &
```

4. 브라우저에서 `http://새IP:8000`에 접속합니다. 안 열리면 로그를 확인합니다.

```bash
tail -30 ~/messenger/app.log
```

**서버를 끌 때**: 앱은 `pkill -f "uvicorn main:app"`로 끄고, 하루 작업이 끝나면 EC2 인스턴스를 **중지**해 Lab 예산을 아낍니다.

**새 코드를 올릴 때**

1. 내 PC의 프로젝트 폴더에서 ZIP을 만들고 서버로 올립니다. `.env`, `data`, `.venv`는 ZIP에 들어가지 않으므로 서버의 것이 유지됩니다.

```powershell
python -m scripts.package_release
scp -i "키파일경로\키이름.pem" dist\messenger.zip ubuntu@새IP:~/
```

2. 서버에서 풀고 라이브러리를 맞춘 뒤 앱을 다시 시작합니다. `requirements.txt`나 `requirements-stt.txt`가 바뀌지 않았다면 `pip install`은 생략해도 됩니다.

```bash
pkill -f "uvicorn main:app"; unzip -o ~/messenger.zip -d ~/messenger && cd ~/messenger && .venv/bin/pip install -r requirements.txt -r requirements-stt.txt
cd ~/messenger && nohup .venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000 > app.log 2>&1 &
```

**음성·영상 인식(Whisper) 처음 설정** (서버마다 한 번만)

AWS Learner Lab은 Transcribe를 지원하지 않아 EC2 안에서 Whisper 모델(faster-whisper)을 직접 실행합니다. AWS 서비스 호출이나 API 키는 필요 없습니다.

```bash
cd ~/messenger && .venv/bin/pip install -r requirements-stt.txt
cd ~/messenger && .venv/bin/python -m scripts.prepare_stt
printf '\nSTT_BACKEND=whisper\nSTT_MODEL=base\n' >> ~/messenger/.env
```

설정한 뒤 앱을 다시 시작합니다. `STT_BACKEND`를 비워 두면 음성 파일 처리가 "음성 인식이 설정되지 않았습니다"로 실패하고, `fake`로 두면 모델 없이 정해진 가짜 결과가 나옵니다. 로컬 PC(Python 3.14 RC)에서는 `faster-whisper`를 설치할 수 없어 `fake`로만 확인합니다. 서버에서 오류가 나면 `app.log`를 확인합니다. 자주 쓰는 설정(모델 크기, 최대 길이, 업로드 크기)은 `.env.example`에 설명이 있습니다.

**그대로 유지되는 것과 다시 해야 하는 것**

- 유지: 설치한 라이브러리(`.venv`), 내려받은 모델, 서버의 `.env`, `data` 폴더
- 다시 해야 함: 앱 시작 명령, 새 공개 IP 확인
- 처음부터 다시 설정: 인스턴스를 삭제했거나 Lab을 Reset했을 때

### DynamoDB + S3로 옮기기 (서버 한 대)

서버 한 대에서 SQS·Redis 없이 **DynamoDB(계정·방·메시지·요약 목록 등)와 S3(원본·추출 본문·음성 인식 결과·요약 결과 파일)**만 쓰는 구성입니다(`SINGLE_NODE=1`). 여러 대로 나누는 전체 구성은 `docs/learner-lab.md`를 따릅니다.

1. EC2 콘솔에서 인스턴스를 선택 → **작업 → 보안 → IAM 역할 수정** → `LabInstanceProfile`을 연결합니다. 역할이 없으면 서버가 AWS를 부를 수 없습니다(`NoCredentialsError`).
2. 새 코드를 올리고 풉니다(위 "새 코드를 올릴 때"). 아직 앱은 다시 시작하지 않습니다.
3. 서버에서 테이블과 버킷을 만들고 `.env`를 갱신합니다. 이름은 본인 것으로 바꾸고, 버킷 이름은 전 세계에서 유일해야 합니다. 이미 있는 리소스는 그대로 쓰며, 서명 키(`AUTH_SECRET`)는 새로 만들어 `.env`에만 저장하고 화면에 출력하지 않습니다.

```bash
cd ~/messenger && .venv/bin/python -m scripts.setup_aws --table messenger --bucket 내버킷이름 --write-env
.venv/bin/python -m scripts.check_aws
```

4. 기존 로컬 데이터를 옮깁니다. 이미 AWS에 있는 항목은 덮어쓰지 않고 원본은 지우지 않습니다. 먼저 `--dry-run`으로 개수를 확인합니다. 옮기지 않으면 예전에 올린 파일과 계정은 AWS 모드에서 보이지 않습니다.

```bash
cd ~/messenger && .venv/bin/python -m scripts.migrate_to_aws --dry-run
cd ~/messenger && .venv/bin/python -m scripts.migrate_to_aws
```

5. 앱을 다시 시작합니다. 서명 키가 바뀌므로 **모두 한 번 다시 로그인**합니다.

```bash
pkill -f "uvicorn main:app"; cd ~/messenger && nohup .venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000 > app.log 2>&1 &
```

6. 새 파일을 올려 S3에 생기는지 확인합니다(콘솔의 버킷 또는 `tail ~/messenger/app.log`).

주의할 점

- 로컬 모드로 되돌리려면 `.env`의 `DATA_BACKEND=sqlite`로 바꾸고 재시작합니다. 로컬 `data` 폴더는 지우지 않았으므로 그대로 쓸 수 있습니다(AWS에서 새로 생긴 데이터는 제외).
- S3 버킷의 CORS는 서버 IP가 바뀌어도 업로드되도록 모든 출처를 허용합니다. 버킷은 퍼블릭 액세스가 모두 차단되어 있고 업로드·재생은 서명된 임시 주소로만 됩니다.
- 음성 재생은 서버가 중계하지 않고 S3의 임시 주소로 직접 재생됩니다.
- Learner Lab 세션이 끝나도 DynamoDB와 S3의 데이터는 유지되지만, **Reset**하면 모두 삭제됩니다.

## 폴더

```text
main.py                     uvicorn main:app 진입점
app/
  main.py                   라우터·정적 파일·상태 검사·서비스 수명 관리
  core/                     설정·SQLite/DynamoDB 트랜잭션·서비스 구성
  auth/                     계정 저장·비밀번호·JWT·로그인 실패 제한
  rooms/                    방 목록·생성·입장·나가기·생성자 전용 삭제
  chat/                     메시지 저장·WebSocket·Redis·역할·읽음
  users/                    언어 설정 API·저장·복원
  media/                    로컬/S3 업로드·완료 확인·추출·영속 작업·SQS 워커
frontend/
  index.html                화면 마크업
  static/
    app.js                  초기화·로그인 복원
    auth/ rooms/ chat/      기능별 JS/CSS
    media/ users/           파일 카드·업로드·언어 설정
    i18n/ shared/           언어 문구·공통 상태·API·스타일
scripts/                    AWS 연결 검사·배포 ZIP 생성
deploy/                    ALB·채팅/워커 ASG CloudFormation 템플릿
docs/                      요구사항 대응·데이터 모델·Lab 연결·복구 절차
tests/                     통합·영속성·워커·이벤트·프런트엔드 검사
```

프런트엔드는 빌드 도구 없이 실행합니다. 기능 파일은 `index.html`의 `defer` 순서로 공통 상태를 공유하며, 초기화 파일 `app.js`를 마지막에 로드합니다.

## 추가한 API

- `GET/PATCH /api/users/me/preferences`: UI·원문·번역·자막 언어 설정.
- `DELETE /api/rooms/{roomId}`: 생성자만 방 삭제. 참여자의 연결과 파일 접근을 차단합니다.
- `GET /api/rooms/{roomId}/members`: 역할과 읽음 순번.
- `POST /api/rooms/{roomId}/read`: 마지막 읽은 순번을 앞으로만 갱신.
- `POST /api/files/{mediaId}/complete`: 직접 업로드 완료 확인. 재호출해도 파일 카드가 중복 저장되지 않습니다.
- `POST /api/ai/translate`: 채팅 메시지 번역(`roomId`, `messageSeq`, `target`). 서버가 저장된 메시지를 직접 읽어 번역하므로 방 멤버만 쓸 수 있습니다.
- `POST /api/ai/translate-page`: 업로드한 문서의 한 페이지 번역(`mediaId`, `page`, `target`). 긴 페이지는 문단 단위로 나눠 번역합니다.
- `GET /health/live`, `GET /health/ready`: 실행·DB/Redis 준비 상태.

기존 인증·방·파일·미디어·WebSocket 주소도 유지합니다. 방 삭제는 논리 삭제이며 메시지·원본·기존 결과를 물리 삭제하지 않습니다.

## AWS 연결과 남은 범위

`.env.example`을 `.env`로 복사하면 앱과 별도 워커가 설정을 읽습니다. 로컬 모드는 별도 AWS 설정이 필요 없습니다. Lab은 `DATA_BACKEND=dynamodb`와 공용 서명 키·테이블·버킷·큐·Redis 주소를 채워 연결합니다.

- [Learner Lab 연결·배포·장애 복구 절차](docs/learner-lab.md)
- [Notion 요구사항 대응·키 모델·보존 정책·다음 구현 순서](docs/implementation.md)

번역은 AWS Lambda 함수 URL을 호출합니다. Lambda 소스는 별도 배포용 [lambda/translation](lambda/translation/) 폴더에 있습니다. `.env`의 `TRANSLATE_URL`, `TRANSLATE_SECRET`을 채우면 앱 번역이 켜지고, 비워 두면 번역 API만 503을 돌려줍니다. 같은 원문·대상 언어는 DB에 캐시하고, 사용자별 시간당 호출 한도(`TRANSLATE_HOURLY_LIMIT`, 기본 60)가 있습니다. 문서 번역은 페이지 단위로 하며 필요한 조각 수를 먼저 예약하고 실제 호출을 시작한 조각만 사용량에 확정합니다. Lambda 성공 응답은 `translated`, `modelVersion: "v1"`, 정상 `finishReason`(예: `end_turn`), `truncated: false`를 포함해야 합니다. 누락·잘림·비정상 종료는 캐시하지 않고 502로 처리하므로, Lambda 응답도 이 계약에 맞춰야 합니다. URL과 비밀 값은 `.env`나 SSM에만 두고 저장소에 올리지 않습니다.

비동기 AI 작업·선택 자료 요약·이미지 및 혼합 PDF OCR·원문 수정 UI를 추가했습니다. [C·D 실행 및 API 안내](docs/ai-ocr.md)를 참고하세요. OCR 워커에는 별도 모델 의존성 설치가 필요하며 실제 모델 품질과 AWS 배포는 추가 검증 대상입니다. STT·자막 생성과 CloudWatch 상세 지표는 아직 구현하지 않았습니다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node tests\check_frontend.cjs
```

Python 검사는 실제 사용자 데이터와 AWS 리소스에 접근하지 않고 임시 저장소·포트와 Moto/fakeredis 모형을 사용합니다. 프런트엔드 검사는 DOM 모형에서 기능 연결을 확인하며 실제 브라우저 렌더링 검사는 아닙니다. AWS 권한·CORS·실제 EC2/워커 중단은 Lab에서 추가로 검증해야 합니다.
