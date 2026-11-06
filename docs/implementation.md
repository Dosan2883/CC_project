# Notion 요구사항과 이번 구현

기준: [클라우드컴퓨팅 기말 과제](https://app.notion.com/p/40cbcc84e50e80e49376d991601130bc), 확인일 2026-11-05. 문서의 “먼저 추가해야 할 기반 기능” A/B를 우선 구현하고, 여기에 의존하는 설정·역할·읽음·방 삭제를 연결했다. Notion 원문은 수정하지 않았다.

| 요구사항 | 구현 | 검증 및 남은 일 |
| --- | --- | --- |
| A: 계정·방·멤버·메시지·설정·미디어·작업 영속화 | SQLite 기본 모드, DynamoDB 선택 모드. 공용 저장 인터페이스와 버전 조건부 트랜잭션 | 두 저장소의 재연결·조건부 쓰기·롤백 검사. DynamoDB는 Moto 모형으로 검증 |
| A: 다중 서버 메시지와 복구 | Redis Pub/Sub, 서버별 접속 현황, 만료된 서버 제거. 이벤트의 메시지 순서 대신 DB의 순번대로 전달 | 두 ConnectionManager와 모의 Redis에서 메시지 전달·역순 이벤트·접속 해제 검사. 실제 EC2 중단 시험은 미실행 |
| A: 충돌 없는 순번 | 방의 마지막 순번과 메시지·미디어 멱등 키를 원자적으로 저장. 멤버와 방 버전도 확인 | 독립 SQLite 작성자 8개에서 32개 메시지 순번 중복·누락 없음. DynamoDB 트랜잭션 검사 |
| A: ALB·Auto Scaling | HTTPS/HTTP 리디렉션, 준비 상태 검사, 채팅 기본 2대/최대 3대, 별도 워커 1대의 CloudFormation 템플릿 | 템플릿 정적 검사. Learner Lab 권한·네트워크·실제 생성은 미검증 |
| B: S3 직접 업로드 | 서명된 조건부 PUT, 크기·내용 확인, 업로드 완료 API, 중복되지 않는 파일 카드 | S3 모형과 API 검사. 실제 브라우저 CORS·서명 URL은 AWS에서 추가 확인 |
| B: 독립 워커·SQS·복구 | DB에 작업 상태와 임대·재시도 저장. 별도 실행 프로세스, SQS 폴링·가시 시간 연장, 만료 임대 회수, 오래된 워커의 결과 쓰기 차단 | 대기/중단 작업 복구, 중복 수신, 재시도 소진·재처리, SQS 수신·삭제 검사. 실제 강제 종료·SQS DLQ 이동 시험은 미실행 |
| F: 언어 설정 저장·복원 | GET/PATCH `/api/users/me/preferences`, UI/원문/번역/자막 언어 분리, 로그인 시 복원 | 서버 API와 프런트엔드 흐름 검사. UI 번역은 기존 3개 언어, 원문·번역·자막 선택지는 6개 유지 |
| G: 멤버 역할·읽음 | 생성자 owner, 참여자 member, `lastReadSeq`, 읽음 API와 이벤트, 읽음 수·안 읽은 수 표시 | 실제 수신과 읽음을 구분. 보이는 탭에 포커스가 있고 대화 맨 아래일 때만 읽음 갱신. 순번은 뒤로 가지 않음 |
| 생성자 전용 방 삭제 | DELETE API의 생성자 검사, 생성자에게만 버튼 표시, 알림·소켓 종료, 파일 접근 차단 | 다른 사용자의 직접 API 호출 거절, 연결 해제·첨부 접근 취소 검사 |

## 저장 모델

DynamoDB 테이블의 파티션 키는 문자열 `pk`, 정렬 키는 문자열 `sk`다. `version`은 숫자이며 `payload`는 JSON 문자열이다. 비밀번호 salt/hash 같은 bytes는 내부 직렬화 시 base64로 보존한다. 모든 DynamoDB Query는 다음 페이지까지 읽는다.

| 데이터 | pk | sk | 주요 필드/조회 |
| --- | --- | --- | --- |
| 계정 | `USER#{정규화 닉네임}` | `ACCOUNT` | 닉네임, salt, pwHash, iterations. 중복 생성은 조건부 쓰기로 거절 |
| 언어 설정 | `USER#{정규화 닉네임}` | `PREFERENCES` | uiLocale, sourceLanguage, defaultTargetLanguage, preferredSubtitleLanguage |
| 방 목록·순번 | `CATALOG` | `ROOM#{roomId}` | createdBy, visibility, lastMessageSeq, deletedAt. 데모 규모에서 목록을 읽고 권한 필터링 |
| 방 이름 예약 | `ROOM_NAMES` | 정규화 방 이름 | roomId. 방·생성자 멤버 생성과 같은 트랜잭션 |
| 방 멤버 | `ROOM#{roomId}` | `MEMBER#{nickname}` | user, role, lastReadSeq, joinedAt |
| 메시지 | `ROOM#{roomId}` | `MESSAGE#{20자리 순번}` | 원문·원문 언어·발신자·시각·파일 카드. 방의 카운터와 같은 트랜잭션 |
| 파일 카드 중복 방지 | `ROOM#{roomId}` | `MEDIA_MESSAGE#{mediaId}` | messageSeq. 업로드 완료를 여러 번 호출해도 메시지는 하나 |
| 미디어 메타데이터 | `MEDIA` | `ASSET#{mediaId}` | roomId, uploader, 원본 키, 내용 해시, 결과 키, 작업 ID |
| 미디어 작업 | `MEDIA_JOBS` | mediaId | 상태, 시도 횟수, availableAt, leaseToken, leaseUntil, deadLetter |
| 인증·비공개방 실패 제한 | `THROTTLE#{용도}` | 사용자 키 | 최근 실패 시각. 서버 사이에서 공유 |

규모를 키우면 `CATALOG`, `MEDIA`, `MEDIA_JOBS`의 단일 파티션과 목록 조회를 샤딩·인덱스로 확장해야 한다. 현재 구현은 과제 규모의 키/쿼리를 명시적으로 선택한 것이다.

## 삭제·보존·재처리 정책

업로드 완료 시 미디어 상태, 파일 카드 메시지, 메시지 순번, 작업 대기 상태를 한 트랜잭션으로 저장한다. 완료 직후 서버가 중단되거나 SQS 전송이 실패해도 파일 카드와 처리 작업이 함께 남으며, 다른 워커가 대기 작업을 복구한다.

방 삭제는 논리 삭제다. 방 이름 예약을 해제하고 방·메시지 쓰기를 막으며 기존 참여자의 소켓과 첨부 접근 권한을 회수한다. 대기 중 작업은 워커가 발견하면 취소하고, 처리 중 작업도 완료 직전에 방의 삭제 상태를 확인해 결과를 게시하지 않는다.

메시지·멤버 기록·원본·이미 저장된 결과는 물리 삭제하지 않는다. 미완성 업로드와 오래된 작업 결과도 자동 정리하지 않는다. 복구 검증과 추후 보존 정책을 위해 남기는 선택이며, 실제 배포 전 정리 기간과 S3 Lifecycle/삭제 작업을 정해야 한다. S3 서명 업로드 주소는 5분 동안 유효할 수 있으나, 삭제된 방의 업로드 완료와 파일 접근은 서버에서 거절한다.

로컬 워커는 DB의 대기 작업을 폴링한다. SQS 워커는 큐를 수신하며 30초 간격으로 DB의 미처리 작업을 다시 큐에 알린다. DB 기록 후 SQS 전송 전에 서버가 중단되어도 작업이 사라지지 않는다. 중복 알림은 예상된 동작이며 임대 토큰과 완료 상태로 중복 결과 쓰기를 막는다. 임대는 90초, DB 임대 갱신은 20초, SQS 가시 시간 연장은 25초 간격이다.

파일 자체의 오류는 재시도하지 않는다. 일시 오류는 최대 3번 시도하고 지수 백오프를 적용한다. 재시도를 소진하면 DB에서 FAILED/deadLetter로 남긴다. SQS 메시지는 삭제하지 않아 **기존 큐의 RedrivePolicy**가 실제 DLQ로 옮기도록 한다. 로컬 모드에는 SQS DLQ 대신 같은 영속 실패 상태가 있다. 파일 카드의 재시도 버튼으로 해당 작업을 다시 대기 상태로 바꿀 수 있다.

## 다음 구현 순서

2026-11-05 검증: 임시 런타임 패치 없이 Python 테스트 31개, 프런트엔드 DOM 모형 검사, CloudFormation YAML 구조 검사와 배포 ZIP 구성 검사를 통과했다. 기존 Python 3.14.0rc2의 시작 오류는 해당 버전에만 적용하는 의존성 조건으로 해결했다. AWS 실배포와 실제 브라우저 렌더링은 검증하지 않았다.

이번 변경에서 다음 항목은 구현하지 않았다. 기반 기능 완료와 구분한다.

1. C: AI 작업 등록/조회, 텍스트 선택 범위, 외부 처리 동의·사용량 상한, SQS AI/Lambda, 실제 번역·요약과 근거 표시.
2. D/F: 이미지 및 페이지별 OCR, 언어 감지·수정, 원문 버전과 번역 캐시. 혼합 PDF의 OCR도 아직 미구현이다.
3. E: FFmpeg, faster-whisper, 녹취록·시간 구간, 원어/번역 WebVTT와 자막 재생.
4. 운영: CloudWatch 세부 지표·경보, 실제 워커·EC2 중단 시험, 언어별 품질 평가와 발표 자료.

React 전환은 필수 조건이 확정되지 않아 하지 않았다. 번역·요약·이미지·음성·영상 버튼의 준비 중 표시는 유지한다. 실제 AI 호출이나 인식 결과를 흉내 내는 구현은 추가하지 않았다.

참고: [DynamoDB 트랜잭션](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html), [S3 조건부 쓰기](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html), [SQS 가시 시간·heartbeat](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/best-practices-processing-messages-timely-manner.html).
