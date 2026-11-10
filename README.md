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

번역은 AWS Lambda 함수 URL을 호출합니다. `.env`의 `TRANSLATE_URL`, `TRANSLATE_SECRET`을 채우면 켜지고, 비워 두면 번역 API만 503을 돌려줍니다. 같은 원문·대상 언어는 DB에 캐시하고, 사용자별 시간당 호출 한도(`TRANSLATE_HOURLY_LIMIT`, 기본 60)가 있습니다. 문서 번역은 페이지 단위로 하며 필요한 조각 수를 먼저 예약하고 실제 호출을 시작한 조각만 사용량에 확정합니다. Lambda 성공 응답은 `translated`, `modelVersion: "v1"`, 정상 `finishReason`(예: `end_turn`), `truncated: false`를 포함해야 합니다. 누락·잘림·비정상 종료는 캐시하지 않고 502로 처리하므로, Lambda 응답도 이 계약에 맞춰야 합니다. URL과 비밀 값은 `.env`나 SSM에만 두고 저장소에 올리지 않습니다.

요약 AI, OCR, STT, 자막 생성과 CloudWatch 상세 지표는 아직 구현하지 않았습니다. 이번 구현은 문서가 우선순위로 정한 저장·분산 채팅·업로드·워커 기반과 설정·읽음·방 삭제입니다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node tests\check_frontend.cjs
```

Python 검사는 실제 사용자 데이터와 AWS 리소스에 접근하지 않고 임시 저장소·포트와 Moto/fakeredis 모형을 사용합니다. 프런트엔드 검사는 DOM 모형에서 기능 연결을 확인하며 실제 브라우저 렌더링 검사는 아닙니다. AWS 권한·CORS·실제 EC2/워커 중단은 Lab에서 추가로 검증해야 합니다.
