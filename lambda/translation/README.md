# 번역 Lambda

이 폴더에는 앱의 `LambdaTranslator`가 기대하는 요청·응답 약속에 맞춘
Lambda 함수 URL 처리 코드가 있습니다.

## 설정

- 실행 환경: Python 3.12
- 핸들러: `handler.handler`
- `BEDROCK_MODEL_ID`: 선택. 지정하지 않으면 `amazon.nova-micro-v1:0`
- `TRANSLATE_SECRET_PARAMETER`: 필수. API 비밀값을 SecureString으로 저장한
  SSM Parameter Store의 이름
- IAM 권한: 해당 파라미터에 대한 `ssm:GetParameter`와, 설정한 모델에 대한
  `bedrock:InvokeModel`
- Lambda URL은 함수 URL이 요구하는 응답 형식을 사용해야 합니다.

비밀값은 SSM에서 복호화해 읽고, Lambda 실행 환경이 유지되는 동안 캐시합니다.
비밀값을 Lambda 환경 변수나 소스 코드에 넣지 마세요.

## 요청과 응답

앱은 `x-api-secret` 헤더와 아래 JSON 본문을 `POST`로 보냅니다.

```json
{"text":"안녕하세요","target":"English"}
```

성공 응답은 함수 URL 응답 형식을 사용합니다. JSON `body`에는 다음 내용이 담깁니다.

```json
{
  "translated": "Hello",
  "finishReason": "end_turn",
  "truncated": false,
  "modelVersion": "v1"
}
```

`finishReason`은 Bedrock Converse의 `stopReason`을 그대로 옮긴 값입니다.
`truncated`는 그 값이 `max_tokens`일 때만 true입니다. Converse의
`inferenceConfig.maxTokens`는 4000입니다. 앱은 잘렸거나 약속한 형식과 맞지 않는
결과를 캐시에 저장하기 전에 거부합니다.

Lambda 코드는 로컬에서 준비한 것입니다. 대상 계정에 SSM 파라미터, IAM 역할,
모델 사용 권한, 함수 URL을 설정한 뒤에 배포하세요.
