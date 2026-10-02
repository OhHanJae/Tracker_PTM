# PT503 TCP/IP 프로토콜 사용법

## 1. 접속 및 프레임

- 기본 주소: API 서버가 실행 중인 PC의 IP
- 기본 TCP 포트: `8765`
- 인코딩: UTF-8
- 프레임: JSON 1개를 한 줄로 전송하는 JSON Lines 방식 (`\n`으로 종료)
- 프로토콜 이름/버전: `PT503-Control` / `1.0`

TCP 연결 직후 서버는 `service.ready` 이벤트와 현재 상태 이벤트 3개를 전송한다. 클라이언트는 그다음 `protocol.hello`를 보내 프로토콜 버전을 확인한다.

요청 형식:

```json
{"id":1,"command":"protocol.hello","params":{"version":"1.0","client_name":"PLC"}}
```

성공 응답:

```json
{"id":1,"ok":true,"result":{"protocol":"PT503-Control","version":"1.0","client_id":"client-1","states":{"pt503":"connected","motor":"stopped","home":"invalid"}}}
```

오류 응답:

```json
{"id":1,"ok":false,"error":{"code":"INVALID_PARAMS","message":"오류 내용"}}
```

응답과 상태 이벤트는 비동기로 섞여서 수신될 수 있다. 응답은 요청의 `id`로 구분하고, 이벤트는 최상위 `event` 필드로 구분한다.

## 2. 명령

### Hello — `protocol.hello`

첫 연결 직후 호출한다. 지원 버전은 `1.0`이다.

```json
{"id":1,"command":"protocol.hello","params":{"version":"1.0","client_name":"PLC"}}
```

지원하지 않는 버전이면 `UNSUPPORTED_VERSION` 오류를 반환한다. 기존 클라이언트 호환을 위해 Hello를 보내지 않은 연결도 즉시 끊지는 않는다.

### Heartbeat — `system.heartbeat`

전송한 값은 `echo`로 그대로 돌려준다.

```json
{"id":2,"command":"system.heartbeat","params":{"sequence":15,"client_time":1790900000.0}}
```

```json
{"id":2,"ok":true,"result":{"alive":true,"echo":{"sequence":15,"client_time":1790900000.0},"server_time":1790900000.1}}
```

### 허용 오차 범위 설정 — `motion.tolerance`

```json
{"id":3,"command":"motion.tolerance","params":{"tolerance_deg":0.2,"stable_samples":3,"timeout_s":30}}
```

- `tolerance_deg`: 목표 도달 허용 오차, `0.01`~`10`도
- `stable_samples`: 허용 오차 안에 연속으로 들어와야 하는 횟수, `1`~`10`
- `timeout_s`: 이동 완료 제한 시간, `2`~`120`초
- 성공 시 현재 적용값을 그대로 에코한다.

### 레시피 설정 — `recipe.select`

```json
{"id":4,"command":"recipe.select","params":{"recipe_id":"recipe-id"}}
```

성공 시 선택된 `recipe_id`와 이름을 반환한다. 레시피를 바꾸면 기존 포인트 선택은 해제된다.

### 포인트 설정 — `point.select`

```json
{"id":5,"command":"point.select","params":{"recipe_id":"recipe-id","point_id":"point-id"}}
```

`recipe_id`를 생략하면 현재 선택 레시피를 사용한다. 성공 시 선택된 레시피/포인트 번호와 이름을 에코한다.

### 지령 이동 요청 — `motion.goto`

현재 선택값으로 이동:

```json
{"id":6,"command":"motion.goto","params":{}}
```

요청에서 직접 지정하여 이동:

```json
{"id":6,"command":"motion.goto","params":{"recipe_id":"recipe-id","point_id":"point-id"}}
```

명령이 장치에 접수되면 먼저 `accepted:true` 응답이 온다. 실제 위치 피드백이 허용 오차 안에서 안정되면 같은 클라이언트에 완료 이벤트가 온다.

```json
{"event":"motion.completed","data":{"request_id":6,"command":"motion.goto","recipe_id":"recipe-id","point_id":"point-id"},"timestamp":"2026-10-02T14:00:00.000+09:00"}
```

시간 초과 또는 통신 실패 시 다음 형식의 이벤트가 온다.

```json
{"event":"motion.error","data":{"request_id":6,"command":"motion.goto","recipe_id":"recipe-id","point_id":"point-id","error":{"code":"MOTION_TIMEOUT","message":"target position was not reached in time"}},"timestamp":"2026-10-02T14:00:30.000+09:00"}
```

### 모터 이동 강제 중단 — `motion.force_stop`

```json
{"id":7,"command":"motion.force_stop","params":{}}
```

중단 프레임 전송 성공 시 `completed:true`, `stopped:true`를 반환한다. 진행 중이던 이동 요청에는 `MOTION_STOPPED` 오류 이벤트가 전송된다. 장치 통신 실패 시 일반 오류 응답을 반환한다.

### 호밍 요청 — `home.start`

```json
{"id":8,"command":"home.start","params":{}}
```

Pan `0°`, Tilt `0°`로 이동하며, 접수 시 `homing:true`를 반환한다. 도달하면 `motion.completed`, 실패하면 `motion.error` 이벤트가 온다.

현재 장치 프로토콜에 별도 원점 센서 응답이 없으므로, 원점 유효 상태는 실제 Pan/Tilt 피드백이 `0°/0°`에서 설정 허용 오차 안에 있는지로 판정한다.

### 재시작 요청 — `maintenance.restart`

```json
{"id":9,"command":"maintenance.restart","params":{"confirm":true}}
```

안전 확인을 위해 `confirm:true`가 필수다. PT503 장치에 재시작 명령 전송 성공 시 `accepted:true`, `restarting:true`를 반환한다.

## 3. 상태 변화 이벤트

연결 직후 현재값을 1회 보내고, 이후 값이 바뀔 때마다 모든 TCP 클라이언트에 전송한다.

| 구분 | 이벤트 | `state` 값 |
|---|---|---|
| PT503 통신 상태 | `pt503.connection_changed` | `connected`(연결됨), `connecting`(연결중), `disconnected`(연결안됨) |
| 모터 상태 | `motor.state_changed` | `moving`(이동중), `stopped`(정지) |
| 원점 상태 | `home.state_changed` | `valid`(유효), `invalid`(무효) |

예시:

```json
{"event":"pt503.connection_changed","data":{"state":"connected"},"timestamp":"2026-10-02T14:00:00.000+09:00"}
{"event":"motor.state_changed","data":{"state":"moving"},"timestamp":"2026-10-02T14:00:01.000+09:00"}
{"event":"home.state_changed","data":{"state":"valid"},"timestamp":"2026-10-02T14:00:05.000+09:00"}
```

## 4. 권장 처리 순서

1. TCP `8765` 포트에 연결한다.
2. `service.ready`와 초기 상태 이벤트를 읽는다.
3. `protocol.hello`로 버전을 협상한다.
4. 일정 주기로 `system.heartbeat`를 요청한다.
5. `motion.tolerance`, `recipe.select`, `point.select`를 설정한다.
6. `motion.goto` 또는 `home.start`를 요청한다.
7. 최초 응답의 `accepted`와 이후 `motion.completed`/`motion.error` 이벤트를 모두 처리한다.
8. 연결이 끊기면 재접속 후 Hello와 선택값 설정을 다시 수행한다. 선택값은 TCP 연결별 값이 아니라 현재 API 서버의 공용 운전값이다.

## 5. Python 접속 예제

```python
import json
import socket

with socket.create_connection(("192.168.0.10", 8765), timeout=3) as sock:
    stream = sock.makefile("rwb")

    # service.ready + 초기 상태 이벤트는 별도 수신 루프에서 처리한다.
    request = {
        "id": 1,
        "command": "protocol.hello",
        "params": {"version": "1.0", "client_name": "PLC"},
    }
    stream.write((json.dumps(request) + "\n").encode("utf-8"))
    stream.flush()

    while True:
        message = json.loads(stream.readline().decode("utf-8"))
        if message.get("id") == 1:
            print("Hello response:", message)
            break
        print("Event:", message)
```
