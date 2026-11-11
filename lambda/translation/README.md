# Translation Lambda

This folder contains the Lambda Function URL handler that matches the app's
`LambdaTranslator` contract.

## Configuration

- Runtime: Python 3.12
- Handler: `handler.handler`
- `BEDROCK_MODEL_ID`: optional; defaults to `amazon.nova-micro-v1:0`
- `TRANSLATE_SECRET_PARAMETER`: required SSM Parameter Store name for the
  SecureString API secret
- IAM permissions: `ssm:GetParameter` for that parameter and
  `bedrock:InvokeModel` for the configured model
- The Lambda URL must use the response format expected by Function URLs.

The secret is fetched from SSM with decryption and cached for the lifetime of a
warm execution environment. Do not place the secret value in Lambda environment
variables or source code.

## Request and response

The app sends `POST` with an `x-api-secret` header and JSON body:

```json
{"text":"안녕하세요","target":"English"}
```

Successful responses use the Function URL envelope. The JSON `body` contains:

```json
{
  "translated": "Hello",
  "finishReason": "end_turn",
  "truncated": false,
  "modelVersion": "v1"
}
```

`finishReason` copies Bedrock Converse's `stopReason`. `truncated` is true only
when that reason is `max_tokens`. The Converse `inferenceConfig.maxTokens` is
4000. The app rejects a truncated or contract-mismatched result before caching
it.

The Lambda code is prepared locally. Deploy it only after configuring the SSM
parameter, IAM role, model access, and Function URL for the target account.
