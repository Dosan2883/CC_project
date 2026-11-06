# 기존 Learner Lab 리소스에 연결

리소스 이름은 코드에 고정하지 않는다. 이 저장소에서 AWS 배포나 리소스 생성은 실행하지 않았다. 이름과 자격 증명이 준비되면 아래 순서로 연결한다.

## 데이터 서비스 조건

- DynamoDB: 문자열 파티션 키 `pk`, 문자열 정렬 키 `sk`를 가진 테이블. `docs/implementation.md`의 데이터 모델을 사용한다. 기존 테이블의 키가 다르면 별도 테이블 또는 마이그레이션이 필요하다.
- S3: 비공개 버킷, 퍼블릭 액세스 전체 차단. 브라우저에서 앱의 실제 HTTPS 출처로 PUT 요청할 수 있게 CORS 설정을 적용한다.
- SQS: Standard 미디어 큐, Visibility Timeout 90초 이상, 별도 DLQ와 RedrivePolicy. `maxReceiveCount` 초기값은 5로 둔다. DLQ에는 더 긴 보존 기간을 지정한다.
- Redis/ElastiCache: 모든 채팅 EC2와 미디어 워커가 같은 엔드포인트와 네임스페이스에 접근할 수 있어야 한다. TLS가 설정된 엔드포인트는 `rediss://`를 쓴다.
- EC2 인스턴스 프로필: 기존 Lab 프로필을 사용한다. DynamoDB 읽기·쿼리·조건부 트랜잭션, S3 읽기·쓰기, SQS 송수신·삭제·가시 시간 변경, 배포 ZIP 읽기와 환경설정용 SSM 복호화 권한을 Lab에서 확인한다.

S3 CORS 예시의 `https://YOUR_APP_HOST`는 실제 도메인으로 바꾼다. 앱 서버의 Authorization 헤더는 S3 업로드에 보내지 않는다.

```json
[
  {
    "AllowedOrigins": ["https://YOUR_APP_HOST"],
    "AllowedMethods": ["PUT"],
    "AllowedHeaders": ["content-type", "if-none-match", "content-length"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 300
  }
]
```

## 환경설정

`.env.example`을 `.env`로 복사하고 이름을 채운다. 앱과 별도 워커 모두 프로젝트 루트의 `.env`를 읽는다. 프로세스 환경변수가 `.env`보다 우선한다.

```dotenv
DATA_BACKEND=dynamodb
AWS_REGION=YOUR_REGION
DYNAMODB_TABLE=YOUR_TABLE
S3_BUCKET=YOUR_PRIVATE_BUCKET
MEDIA_QUEUE_URL=YOUR_QUEUE_URL
REDIS_URL=rediss://YOUR_REDIS_HOST:6379/0
AUTH_SECRET=YOUR_SHARED_RANDOM_SIGNING_SECRET
RUN_MEDIA_WORKER=0
```

AWS 모드는 공용 서명 키·S3·SQS·Redis 중 하나라도 없으면 시작 시 오류로 알린다. 서명 키는 모든 웹 서버에서 같은 무작위 값을 사용한다. 로컬 모드에서는 `data/messenger.sqlite3`에 서명 키도 저장하므로 재시작해도 기존 로그인 토큰이 유지된다.

로컬 PC의 Lab 자격 증명은 AWS 프로필이나 `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`으로 공급한다. EC2에서는 인스턴스 프로필을 사용한다. 키는 코드·배포 ZIP에 넣지 않는다.

```powershell
.\.venv\Scripts\python.exe -m scripts.check_aws
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

별도 터미널/EC2에서 같은 환경설정으로 워커를 실행한다.

```powershell
.\.venv\Scripts\python.exe -m app.media.worker
```

`check_aws`는 읽기 검사만 수행한다. 성공해도 쓰기 권한·S3 브라우저 업로드·장애 복구까지 검증된 것은 아니다.

## ALB·채팅 Auto Scaling·워커 배포

`deploy/learner-lab.yaml`은 **검토용 CloudFormation 템플릿**이다. 기존 VPC·서브넷·ACM 인증서·Lab 인스턴스 프로필·데이터 서비스를 입력받으며 새 IAM 역할은 만들지 않는다. 채팅은 기본 2대/최대 3대, 워커는 별도 1대다. ALB는 HTTPS를 제공하고 HTTP를 HTTPS로 보낸다. CPU 목표 추적 정책과 `/health/ready` 검사를 포함한다.

1. 정식 Python 3.11 이상이 있는 로컬/개발 환경에서 의존성과 테스트를 확인한다.
2. `python -m scripts.package_release`로 `dist/messenger.zip`을 생성하고 본인의 배포용 S3 위치에 업로드한다. ZIP에는 앱 코드·화면·실행 의존성만 포함된다.
3. 위 dotenv 내용을 기존 SSM SecureString에 저장한다. `DATA_BACKEND=dynamodb`, `RUN_MEDIA_WORKER=0`을 포함한다. 이 파라미터의 이름을 템플릿에 넣는다.
4. 템플릿에 VPC, ALB용 서로 다른 가용 영역의 공개 서브넷 2개 이상, 인스턴스용 서브넷, ACM 인증서 ARN, 프로필 이름, 배포 ZIP 위치를 넣는다. 인스턴스 서브넷에는 AWS와 패키지 저장소로 나갈 경로가 필요하다.
5. CloudFormation 변경 세트와 Learner Lab 제한·예상 리소스를 확인한 후 적용한다. 템플릿은 ALB·보안 그룹·EC2 ASG를 생성한다. 이 문서를 작성하면서 적용하지는 않았다.
6. 스택의 InstanceSecurityGroupId가 기존 Redis 보안 그룹의 Redis 포트에 접근하도록 연결한다. 보안 그룹과 네트워크 경로가 준비되어야 앱이 정상 시작한다.
7. 본인 도메인을 ALB에 연결하고 인증서·S3 CORS 출처를 맞춘다. ALB의 원래 DNS 이름만으로는 본인 도메인용 인증서가 검증되지 않는다.
8. 두 채팅 EC2의 Target Health와 워커의 `journalctl -u messenger`를 확인한다. 새 릴리스는 새 S3 키로 템플릿을 갱신한 다음 ASG Instance Refresh로 반영한다.

Lab이 SecureString 복호화·ElastiCache·Auto Scaling·특정 인스턴스 유형 등을 막으면 권한에 맞춰 구성을 조정해야 한다. AWS 계정에서의 CloudFormation 검증과 실제 배포는 아직 수행하지 않았다.

## 장애·복구 확인

1. 서로 다른 채팅 서버에 접속한 두 사용자로 메시지·읽음 이벤트를 확인한다. ALB에서는 세션 고정을 설정하지 않는다.
2. 계정·방·설정·대화를 만든 뒤 서버를 재시작한다. 같은 키로 로그인과 대화가 복원되는지 확인한다.
3. 채팅 EC2 하나를 중단한다. 브라우저의 재접속과 마지막 순번 이후 누락 메시지 복구를 확인한다. Pub/Sub 재연결 중 놓친 메시지도 DB 재조회로 복구한다.
4. S3 PUT 후 완료 API를 반복 호출해도 파일 메시지가 한 개임을 확인한다. 업로드 재사용에 의한 원본 덮어쓰기는 조건부 PUT으로 거절되어야 한다.
5. 작업 처리 중 워커를 중단한다. 임대 만료 후 다른 워커가 이어받고 결과가 한 번만 게시되는지 확인한다.
6. 일시 오류를 반복 발생시켜 FAILED/deadLetter 상태와 실제 SQS DLQ 이동을 확인한다. 오류 원인을 해결한 후 파일 카드의 재시도로 재처리를 확인한다.
7. 생성자 외의 사용자가 DELETE API를 호출하면 403인지, 생성자 삭제 후 다른 서버의 소켓·파일 접근도 차단되는지 확인한다.

현재 검증은 로컬 HTTP/WebSocket, SQLite 및 모의 DynamoDB·S3·SQS·Redis까지다. AWS 지연·권한·CORS·강제 중단 결과와 CloudWatch 상세 지표는 별도로 측정해야 한다.
