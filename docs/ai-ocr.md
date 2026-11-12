# C·D 구현과 실행 (2026-11-11)

## AI 작업

`POST /api/ai/jobs`는 `roomId`, `kind` (`translate`, `summarize`, `meeting-summary`),
`sourceLanguage`, `target`, `consent:true`, `sources`를 받는다. 서버가 원문을 조회한다.

```json
{"roomId":"방 ID","kind":"summarize","sourceLanguage":"auto","target":"ko","consent":true,"sources":[{"kind":"message","messageSeq":1},{"kind":"page","mediaId":"파일 ID","page":1,"startChar":0,"endChar":100}]}
```

녹취록은 저장된 `segments:[{start,end,text}]`에서 `kind:segment`, `mediaId`, `start`,
`end`로 선택한다. STT 생성은 E의 별도 구현이며 이 API는 존재하는 녹취만 읽는다.
한 요청은 최대 20개 자료·3500자다. 긴 문서는 페이지 또는 문단을 나누어 요청한다.
조회는 `GET /api/ai/jobs/{jobId}`, 실패 재요청은 `POST .../{jobId}/retry`다.
요청자와 방 멤버를 모두 검사하고 원문 버전이 달라지면 409로 오래된 결과 표시를 막는다.
요약은 [1] 형식의 근거 번호를 요구하고 존재하지 않는 번호가 있으면 실패 처리한다.
근거 원문·메시지 시각·페이지·녹취 구간도 결과에 저장한다. 생성 요약의 사실 정확도는 별도 평가가 필요하다.

기본 로컬 실행은 영속 DB의 작업을 앱 워커가 처리한다. `AI_QUEUE_URL`을 설정하면
SQS로 등록하고 `lambda/ai_jobs/handler.py`를 SQS 이벤트 소스로 연결한다.
`deploy/ai-jobs.yaml`은 큐·DLQ·이벤트 매핑·1분 복구 스케줄을 구성한다.
AI Lambda 배포 ZIP에는 `app/`, 루트 `requirements.txt`의 의존성과
`lambda/ai_jobs/handler.py`를 루트 `handler.py`로 포함해야 한다.
기존 실행 역할에는 DynamoDB 트랜잭션, S3 결과 읽기/쓰기, SQS 수신·삭제·전송·가시시간 권한이 필요하다.
앱에도 출력된 AIQueueUrl을 설정한다. 번역용 Lambda에는 업데이트한
`lambda/translation/handler.py`를 배포해야 `action:summarize`를 처리한다.

외부 오류는 최대 3번 재시도하고 2·4·8초 지수 백오프를 사용한다. 반복 실패는
DB deadLetter와 SQS RedrivePolicy로 처리한다. 만료된 실행 lease는 복구 스케줄이 다시 등록한다.
`AI_LANGUAGE_LIMITS`는 대상 언어별 시간당 호출 수와 토큰 예산을 설정한다.
기본값은 60회·120000 토큰이며 입력 UTF-8 바이트 + 최대 출력 4000 토큰을 보수적으로 예약한다.
실패한 외부 호출도 예산을 소비하며 번역의 기존 사용자 한도도 유지된다.

## OCR

미디어 워커는 정식 Python 3.11/3.12에서 다음 의존성을 설치한다.

```powershell
python -m pip install -r requirements.txt -r requirements-ocr.txt
python -m app.media.worker
```

PaddleOCR 모델은 최초 실행 때 내려받으므로 워커에서 미리 모델을 준비한다.
현재 작업 폴더의 Python 3.14 RC에는 PaddleOCR 실행 환경을 설치하지 않았다.
모델 미설치·실패를 성공 텍스트로 가장하지 않고 `OCR_NOT_INSTALLED`/`OCR_FAILED`,
`needsReview`로 표시한다. 직접 수정하거나 언어를 지정해 다시 처리할 수 있다.

기본 EC2 배포 경로는 `requirements-ocr.txt`를 ZIP에 포함하고 worker 노드에서 설치한다.
`scripts/prepare_ocr.py`가 언어별 모델을 순차 준비하여 manifest를 기록하고 서비스 시작 전
`--check`로 버전·캐시 준비 상태를 확인한다. 실제 신규 EC2와 샘플 이미지 검증은 별도로 수행한다.

JPG/JPEG/PNG 형식·실제 이미지 헤더·최대 2500만 픽셀을 검사한다.
EXIF 방향·RGB·대비를 정규화하고 PDF는 텍스트가 빈 페이지만 렌더링하여 OCR한다.
auto 모드는 6개 언어 모델 후보의 평균 신뢰도로 선택하며 낮은 신뢰도와 auto 결과는
원문 확인 대상으로 표시한다. 이 선택은 정확한 언어 감지기의 대체가 아니므로 수동 언어 지정을 지원한다.
결과는 페이지 번호·영역 polygon·신뢰도·언어·모델·이미지 크기와 함께 저장한다.

`POST /api/media/{id}/process?language=ko`로 명시적 언어 재처리를 요청한다.
`GET /api/media/{id}/pages/{page}/image`는 멤버 권한 확인 후 PNG를 제공한다.
`PATCH /api/media/{id}/pages/{page}`는 `{version,text,language}`로 수정한다.
원본 파일과 최초 추출 텍스트는 보존하며 새 결과 키·버전으로 저장한다.
동시 수정은 409이고 처리 중 수정한 원문을 오래된 OCR 작업이 덮어쓰지 않는다.
파일 본문의 선택 범위는 저장된 최신 원문을 기준으로 번역·요약한다.

검증: 전체 회귀 56개 및 프런트엔드 DOM 검사 통과 후 C·D 전용 7개 검사를 통과했다.
전용 검사는 가짜 AI/OCR 모델, 실제 SQLite·이미지 검증·PDF 렌더링을 사용한다.
실제 PaddleOCR 한국어/영어/일본어 품질, 외부 Bedrock, SQS/Lambda 배포는 별도 검증 대상이다.

PaddleOCR API 근거: https://www.paddleocr.ai/v3.0.0/en/version3.x/pipeline_usage/OCR.html
