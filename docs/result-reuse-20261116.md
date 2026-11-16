# 2026-11-13 변경 요청 구현 (2026-11-16)

## 저장 결과 우선 사용

작업 종류·방·자료 및 범위·원문 해시·원문 언어·대상 언어·원문/추출 버전·모델/프롬프트 버전으로 resultId를 만든다. 자료 순서는 정규화한다. 동일 요청은 같은 jobId를 사용한다. S3(로컬 모드는 LocalStorage)의 완료 파일을 먼저 읽으며, 유효하면 새 작업 등록·AI 호출·한도 차감 없이 반환한다. 동기 번역 API와 비동기 번역 버튼도 같은 결과 경로를 사용한다.

```text
rooms/{roomId}/room.json
rooms/{roomId}/summaries/{resultId}/result.json
rooms/{roomId}/messages/{seq}/translations/{resultId}/result.json
rooms/{roomId}/files/{mediaId}/original.pdf
rooms/{roomId}/files/{mediaId}/extracted/{version}/...
rooms/{roomId}/files/{mediaId}/translations/{resultId}/result.json
rooms/{roomId}/files/{mediaId}/summaries/{resultId}/result.json
rooms/{roomId}/files/{mediaId}/summaries/{resultId}/chunks/{chunkId}.json
```

DB에는 AI_ARTIFACTS의 조건부 lease·저장 대기 응답, AI_JOBS의 작업 상태·resultKey, ROOM_RESULTS의 방별 목록이 있다. AI 응답을 DB에 저장한 뒤 S3에 쓰고 완료를 확정한다. S3 쓰기만 실패하면 저장 대기 응답으로 재시도한다. S3 쓰기 후 DB 완료가 실패하면 파일을 찾아 복구한다. S3 조회/권한 오류는 RESULT_STORAGE_UNAVAILABLE로 처리하며 새 AI 호출을 시작하지 않는다.

외부 호출 후 응답을 영속 저장하기 전에 프로세스가 종료된 경우는 정확한 호출 여부를 확정할 수 없다. CALLING 상태에서 복구 근거가 없으면 AI_CALL_UNCERTAIN으로 자동 재호출을 중단한다. 운영자가 저장·외부 호출 상태를 확인해야 한다. 이 경우까지 정확히 한 번 과금을 보장하지 않는다.

기존 원본·추출 키는 읽을 수 있고 접근 시 새 prefix로 복사하여 이전 파일도 보존한다. 검증 가능한 이전 요약 파일/DB 캐시와 번역 DB 캐시도 재사용한다. 방 생성은 DB에 marker 저장 대기 상태를 원자적으로 남긴 뒤 room.json을 저장하며 실패 시 30초 복구 작업이 재처리한다. 빈 요약 완료 파일은 만들지 않는다.

## PDF·화면

업로드는 파일 검증·카드 등록까지만 한다. 원본 보기는 인증된 `GET /api/media/{id}/original`의 PDF/이미지를 표시하며 추출이나 AI를 실행하지 않는다. 번역/요약 버튼에서만 추출을 요청한다. 이미 있는 추출문을 재사용하고 같은 파일의 추출 작업을 공유한다. UPLOADED는 정상 대기 상태이므로 이 상태를 계속 폴링하지 않는다.

파일 카드에서 전체 문서/페이지를 구분하며 추출문에서는 선택 범위를 지정한다. `kind:document`는 모든 페이지를 읽는다. 전체 문서에 빈 스캔 페이지가 있으면 OCR_REQUIRED로 중단해 부분 텍스트를 전체 요약으로 표시하지 않는다.

긴 자료는 최대 40,000자, 입력 선택 자료 최대 20개, 분할 입력 최대 16조각이다. 2800자 조각을 요약하고 긴 조각 결과는 최대 4단계로 종합한다. 완료 조각은 파일로 재사용한다. 실제 외부 호출은 작업당 최대 32회이며 언어별 시간당 예산도 적용한다. 중간 실패 시 성공 조각은 다시 호출하지 않는다. 모든 조각과 종합이 성공해야 완료다. 한도를 넘으면 범위를 줄여 요청한다. AI Lambda timeout은 900초, SQS visibility는 5400초이며 DB lease를 갱신한다.

회의 요약 버튼은 오른쪽 저장 목록을 연다. +로 선택을 시작하고 선택 수·문자 수·대상 언어를 확인한 뒤 생성/취소한다. 저장 요약은 현재 방 멤버가 함께 조회한다. 원문 변경 시 목록에 이전 버전임을 표시하고 명시적 이력 조회에서만 보여준다. 이력 근거를 현재 원문 번역으로 바꿔 표시하지 않는다. 방 이동·로그아웃·패널 닫기 후 이전 응답은 무시한다. 근거는 기본 원문이며 추가 번역은 명시 버튼으로만 요청한다.

## API·가입 언어

- `POST /api/ai/jobs`: source kind에 document 추가. 추출 전이면 EXTRACTION_PENDING(409), 추출 상태를 기다린 뒤 같은 요청을 다시 등록한다.
- `GET /api/rooms/{roomId}/ai-results`: 방별 저장 결과 메타데이터.
- `GET /api/rooms/{roomId}/ai-results/{jobId}?historical=true`: 권한 내 이전 버전 결과 조회.
- `POST /api/auth/signup`: uiLocale(ko/en/ja). 계정과 초기 언어 설정을 같은 DB 트랜잭션으로 저장한다. 로그인은 기존 설정을 변경하지 않는다.

검증은 가짜 AI·임시 SQLite·Moto S3/DynamoDB 및 DOM 모형을 사용한다. 실제 S3 배포, 실제 문제 PDF의 번역·요약 품질, 브라우저 PDF 렌더링과 모바일 레이아웃은 별도로 확인해야 한다.
