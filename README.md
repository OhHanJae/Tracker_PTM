# BIT-PT503 / PT510 제어 프로그램

Windows와 Ubuntu 24.04 계열 장비에서 USB-RS485 변환기를 통해 BIT-PT503/PT510을 설정하고 시험하는 프로그램입니다. PySide6 GUI 클라이언트와 PySide 없이 실행되는 헤드리스 API/Web 서버를 분리해서 사용할 수 있습니다.

## 안전 주의

- PT503 전원은 외부 `DC 24V` 파워서플라이에서 공급합니다.
- RainbowLink의 12V/5V/3.3V 출력으로 PT503을 구동하지 마세요.
- 전원을 켜거나 Self-check를 실행하면 Pan/Tilt가 자동으로 움직일 수 있습니다.
- `Auto Home OFF`는 무조작 복귀 기능만 끕니다. 전원 인가 시 보정 동작은 별도 기능입니다.
- 장비와 탑재물을 단단히 고정하고 이동 범위의 사람·케이블·공구를 치우세요.
- 처음에는 낮은 속도로 방향과 리미트를 확인하세요.
- 연결 해제와 프로그램 종료 시 STOP 명령을 자동 전송하지만, 안전장치나 비상정지를 대체하지 않습니다.
- 게임패드 제어 활성화 전에는 스틱을 중앙에 놓으세요. 활성화 직후에도 중앙값을 한 번 확인해야 Jog가 시작됩니다.

## 배선

### 본체 전원

| PT503 하부선 | 파워서플라이 |
|---|---|
| 굵은 갈색 DC24V | V+ |
| 굵은 파란색 GND | V- |
| 굵은 검정색 Shell GND | PE/FG/접지 |

### RainbowLink TEL0185 RS485

| PT503 하부선 | RainbowLink |
|---|---|
| 가는 주황색 RS485_1A | A |
| 가는 노란색 RS485_1B | B |
| 가는 검정색 RS485_1G | GND |

PT503의 굵은 검정색 Shell GND는 RainbowLink GND가 아니라 파워서플라이 PE에 연결합니다. 응답이 없으면 송신을 멈춘 상태에서 A/B만 서로 바꿔 확인하세요.

## 가장 쉬운 실행 방법

### Windows

1. Python 3.10 이상 64비트를 설치합니다. 설치할 때 Python Launcher를 포함하세요.
2. 장비가 연결된 PC에서는 `run_server.bat`을 실행합니다.
3. 조작 PC에서는 `run_client.bat`을 실행하고 서버 IP/TCP 포트에 연결합니다.
4. 첫 실행에서는 `.venv` 생성과 의존성 설치 때문에 시간이 걸립니다.

실행 파일 구분:

- `run_server.bat`: 웹 서버 + TCP JSON API 서버만 실행
- `run_client.bat`: PySide6 TCP API 클라이언트만 실행
- `run.bat`: 기존 호환용 실행 파일

직접 설치하려면:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-server.txt
python main.py --server --host auto --tcp-host auto
```

### Ubuntu 24.04 현장 서버

Ubuntu 장비에서는 GUI를 띄우지 않고 웹 콘솔과 TCP JSON 서버만 실행합니다. 외부 PC에서 Ubuntu 장비의 IP로 접속해 설정·제어하는 구조입니다.

```bash
sudo apt update
sudo apt install python3 python3-venv
sudo apt install python3-serial   # 인터넷이 안 되는 현장 장비에서는 이 방식 권장
sudo usermod -aG dialout "$USER"
# 위 dialout 적용을 위해 로그아웃 후 다시 로그인
chmod +x run_linux.sh
./run_linux.sh
```

기본 실행 포트:

- 웹 콘솔/HTTP API: `현재 PC IP:8080`
- TCP JSON Lines API: `현재 PC IP:8765`

외부 PC에서는 브라우저로 `http://Ubuntu장비IP:8080`에 접속하거나, Main APP에서 `Ubuntu장비IP:8765` TCP 서버에 접속합니다. 포트를 바꿔야 하면 `PT503_PORT`, `PT503_TCP_PORT` 환경변수를 사용하거나 `./run_linux.sh --port 9000 --tcp-port 9001`처럼 실행합니다.

PySide6 클라이언트가 필요할 때는 `./run_linux.sh client`, 기존 직접제어 GUI가 정말 필요할 때만 `./run_linux.sh legacy-gui`를 사용합니다.

인터넷/DNS가 막힌 장비에서 `pip`가 `pyserial`을 못 받으면 `python3-serial` 패키지를 설치하세요. 완전 오프라인이면 인터넷 되는 PC에서 `pyserial` wheel 파일을 받아 프로젝트의 `wheels/` 폴더에 넣으면 `run_linux.sh`가 먼저 그 파일로 설치를 시도합니다.

## 헤드리스 API/Web 콘솔

PySide6 없이 API 서버만 켜려면 다음처럼 실행합니다.

```powershell
python -m pip install -r requirements-server.txt
python main.py --server --host auto --port 8080 --tcp-host auto --tcp-port 8765
```

기본 바인딩 `auto`는 현재 PC의 주 LAN IPv4 주소를 사용합니다. 브라우저에서 `http://서버IP:8080`을 열면 통신 연결, Jog/STOP, 절대좌표 이동, 위치 조회, 레시피/포인트 테이블 관리, 포인트 지령위치 이동, 프로토콜 유틸리티 명령 실행을 할 수 있습니다.

Ubuntu 24.04/RDK X5처럼 GUI가 없는 현장 서버 환경에서는 `run_linux.sh`가 웹 서버와 TCP 서버를 외부 접속용으로 함께 실행합니다.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-server.txt
python main.py --server --host auto --port 8080 --tcp-host auto --tcp-port 8765
```

외부 PC에서 접속해야 하므로 신뢰 가능한 폐쇄망에서만 사용하세요. 이 서버에는 별도 인증 기능이 없으므로 사무망/인터넷에 직접 노출하지 마세요.

웹 콘솔은 수동·위치, 레시피·포인트, 게임패드, 레이저 모듈, 프로토콜·고급 설정, 통신 로그 탭으로 구성됩니다. 레시피와 포인트를 추가·수정·삭제하고 순서를 변경할 수 있습니다. 프로토콜 탭은 서버의 전체 명령 목록을 읽어 입력 폼과 JSON 실행을 제공합니다.

수동 속도는 축별 1~8단계(1%, 15%, 29%, 43%, 58%, 72%, 86%, 100%)입니다. 실제 장비 명령은 정수 속도로 반올림됩니다. 절대좌표·포인트·상대이동 등 자동 Pan/Tilt 이동은 최고속도 설정을 전송합니다. 포인트의 과거 속도 필드는 이동 속도를 바꾸지 않습니다.

웹 Jog 갱신 중단 시 기본 500ms watchdog이 STOP을 보냅니다. 위치 모니터링과 목표 도달 판정은 서버에서 실행됩니다. 실물 장비의 응답 및 최고속도 적용 여부는 현장에서 검증해야 합니다.

브라우저 게임패드는 브라우저를 실행하는 외부 PC에 연결합니다. 원격 HTTP 접속에서는 브라우저 보안 정책 때문에 게임패드 API가 제한될 수 있으므로, 실제 현장 조작은 화면 버튼 또는 TCP Main APP 제어를 우선 권장합니다. 상세 운용과 제한은 [웹 콘솔 설명](docs/WEB_CONSOLE.md)을 참고하세요.

## 최초 연결

1. RainbowLink를 PC USB Type-C 포트에 연결합니다.
2. `포트 새로고침`을 누릅니다.
3. RS485 채널 COM 포트를 선택합니다.
4. 기본값 `9600bps / Pelco-D / 주소 1`로 `연결`을 누릅니다.
5. `수동·위치 → 지금 위치 조회`를 누릅니다.
6. RX 로그에 `Pan 위치 응답`이 나오면 정상입니다.

RainbowLink는 최대 4개 COM 포트를 만들 수 있습니다. 공식 드라이버를 설치하면 채널 이름으로 RS485 포트를 구분하기 쉽습니다.

## 외부 Main APP 연동

Ubuntu/Windows 현장 서버에서는 `python main.py --server --host auto --tcp-host auto` 또는 전용 실행 스크립트로 HTTP API/Web 콘솔과 TCP/JSON 제어서버가 함께 동작합니다. PySide6 클라이언트는 별도 프로세스로 실행되어 TCP 서버에 접속합니다. 어느 방식이든 COM 포트는 서버 프로세스 하나만 열어야 합니다.

- UTF-8 JSON 한 개와 줄바꿈(`\n`)을 한 메시지로 사용
- 요청 `id`와 같은 `id`의 성공/오류 응답 제공
- 상태 확인은 `system.status`, `position.get` 응답으로 확인
- 외부 Jog 명령 갱신이 끊기거나 클라이언트가 종료되면 자동 STOP
- 레이저 ON 소유 클라이언트가 종료되면 자동 OFF 요청
- 서버는 COM 연결 상태를 주기적으로 확인하고 끊기면 자동탐색 재시도
- PySide6 클라이언트는 게임패드 연결이 끊기면 자동 재연결 시도
- Python 및 Qt 6/C++ Main APP 클라이언트 예제 포함

전체 명령, 값 범위, 이벤트, 오류 코드와 예제는 [외부 Main APP 제어 API](docs/EXTERNAL_API.md)를 참조하세요.

가장 간단한 확인 방법:

```powershell
python examples\python_api_client.py
```

## 안전 자동검색

`안전 자동검색`은 수동 이동이나 초기화 명령을 보내지 않고 `Pan 위치 조회`만 사용합니다.

- 현재 COM 또는 모든 COM 선택
- 2400/4800/9600/19200bps 선택
- 주소 범위 선택
- 기본 주소 범위는 1~16
- 응답 체크섬이 정상인 장비만 발견 처리
- 전체 COM·주소 1~255 검색은 수 분이 걸릴 수 있음

발견되면 COM, Baud rate와 주소가 UI에 자동 반영되고 해당 포트에 연결됩니다.

## 주요 기능

### 수동·위치

- 버튼을 누르는 동안 Pan/Tilt 이동
- 버튼을 놓으면 즉시 STOP
- 4초마다 Pelco-D runaway protection용 이동 명령 재전송
- Pan/Tilt 방향 반전
- 절대 Pan/Tilt 위치 이동
- Pan/Tilt 위치 조회 및 주기 모니터링
- 위치 자동 모니터링의 주기적 TX/RX는 화면·파일 로그에서 숨김
- 목표좌표와 현재좌표의 오차를 이용한 소프트웨어 이동 완료 판정

Tilt 표시각은 Pelco 관례를 따릅니다.

- `+` : 수평선 위쪽
- `-` : 수평선 아래쪽
- 송신 wire 값은 위쪽 각도를 360° 기준으로 환산

장비 설치 방향에 따라 실제 움직임이 반대라면 먼저 낮은 속도로 확인하세요.

### 레시피·포인트

- 레시피별 포인트 테이블로 Pan/Tilt 좌표 관리
- 포인트 순서 변경, 사용 여부, 속도, 체류시간, 메모 관리
- 현재 위치를 포인트 좌표로 가져오기
- 포인트 이동 시 장비 프리셋 저장/호출을 사용하지 않고 `지령위치 이동` 명령으로 Pan/Tilt 절대좌표 이동
- 프로토콜 시험용 프리셋/AUX/렌즈/원시 HEX 명령은 API 유틸리티로 별도 제공
- 레시피 데이터는 Windows와 Linux 모두에서 JSON 파일로 저장

### 웹 도면 관리·레시피별 캘리브레이션

업데이트할 때는 실행 장비에 최신 PTM 소스를 반영하고 **PTM 서버 프로세스를 완전히 중지한 뒤 재시작**해야 합니다. Python 서버는 시작할 때 도면 처리 코드를 읽으므로 웹 화면만 새로고침하면 이전 서버와 새 화면이 섞일 수 있습니다. 재시작 후 **Ctrl+F5**로 화면을 다시 불러오세요. 구형 서버에서는 도면 화면에 재시작 안내가 표시됩니다. Core가 PTM을 관리하는 경우 Process Manager에서 PTM을 중지한 후 다시 시작합니다.

1. **도면 관리 → 파일 등록**에서 FVP 전체설정 ZIP을 선택합니다. 제품 라이브러리의 GLB 모델과 그룹/레시피 포인트를 가져옵니다. 기존 `GLB + coordinates_manifest.json + group_*.json`과 PTM 백업 ZIP도 지원합니다.
2. 도면에서 **그룹 → 그룹 레시피**를 선택합니다. 같은 그룹에 새 레시피를 추가할 수 있으며, 기준점·티칭 Pan/Tilt·보정 결과·제품 각도는 레시피마다 별도로 저장됩니다.
3. 제품을 **0°/0°**로 놓고 기준점을 최소 4개 티칭한 다음 캘리브레이션합니다. 제품 각도를 바꾸고 다시 계산하면 깊이 변화를 포함한 3D 자세를 적용하며 모델과 포인트도 함께 회전합니다. 기존 방식대로 제품 포인트 범위의 중심을 회전 중심으로, 가장 긴 축/다음 축을 회전축/틸팅축으로 사용합니다. 실제 지그의 중심·축과 일치하는지 현장에서 확인해야 합니다.
4. 계산한 이동 좌표는 **레시피로 저장**으로 반영합니다. 다시 저장해도 기존 포인트 ID·속도·체류시간·사용 여부를 유지합니다. 기준점 재티칭은 제품 각도를 0°/0°로 되돌린 후 수행합니다. 보정 초기화는 선택한 레시피에만 적용됩니다.
5. **도면 관리 → ZIP 내보내기**는 원래 FVP 설정 파일을 보존하고 PTM 레시피/보정 데이터를 추가합니다. 다시 가져오면 그룹별 레시피와 보정이 복원됩니다. 기존 레시피와 ID가 겹치면 복사본으로 등록합니다. 레시피 JSON 내보내기에도 보정 데이터가 포함됩니다.

도면 관리 목록에서 이름을 수정하고 ‘이름 저장’을 누르거나 ‘위로/아래로’로 표시 순서를 바꿀 수 있습니다. 이름·순서는 재시작 후에도 유지되며, 기본 제공 도면도 삭제할 수 있습니다. 삭제한 기본 도면은 다시 자동 등록되지 않습니다. 캘리브레이션 패널과 뷰어의 ‘포인트 ID 보이기’ 체크박스는 같은 표시 상태를 제어합니다.

### 스캔·홈

- 제조사 라인스캔 시작점/종료점 설정
- 제조사 방식 및 표준 Pelco Zone Scan 방식 제공
- 스캔/크루징 속도 조정
- 크루징 경로 1~8 시작
- Auto Home ON/OFF 및 복귀 후 동작 설정

Auto Home은 일정 시간 조작이 없을 때 복귀하는 Guard 기능입니다. 제공 사양서에는 전원 인가 시 별도의 보정용 `Start Movement`가 있다고 명시되어 있으므로 Auto Home을 OFF해도 부팅 시 움직일 수 있습니다. 고급 탭의 `전원 인가 Self-check OFF`는 명령표에 `customized`라고 표시된 `FF Addr 00 05 00 77 checksum`을 전송합니다. 해당 펌웨어에서 지원되지 않거나 부팅 보정이 고정이면 효과가 없으며, 상태 조회 명령이 없어 전원을 재인가해 직접 확인해야 합니다.

Auto Home 대기시간 명령은 제공된 명령표에서 값의 단위와 매핑이 명확하지 않아 전용 입력을 만들지 않았습니다.

### 레이저 모듈

레이저 탭은 Pelco-D AUX ON/OFF를 레이저 제어 채널로 사용합니다.

- ARM/DISARM 없이 ON/OFF 가능 (배포용 GPIO 서버 동작은 아래 RDK 절 참고)
- 50ms~60초 펄스 시험
- 연결 해제와 프로그램 종료 시 AUX OFF 우선 전송
- 화면의 ON/OFF는 출력 피드백이 아니라 마지막 송신 명령 기준

PT503 사진의 배선만으로 실제 AUX 출력 단자는 확인되지 않습니다. 레이저를 통신선이나 AUX 논리선에 직접 연결하지 말고, 제조사가 확인한 AUX 출력 또는 별도 절연 릴레이/MOSFET 인터페이스로 레이저 전원을 스위칭해야 합니다.

### 360 스타일 USB 게임패드

게임패드 탭에서 장치를 새로고침하고 연결한 다음 `게임패드로 장비 제어 허용`을 선택합니다. 게임패드 입력은 `pygame`을 통해 읽으며, Windows와 Ubuntu/Linux에서 먼저 SDL 표준 컨트롤러 매핑을 사용합니다.

| 입력 | 기본 기능 |
|---|---|
| 왼쪽 스틱 X/Y | Pan/Tilt 비례속도 Jog |
| D-pad 왼쪽/오른쪽 | Pan 최대속도 -/+ 2 |
| D-pad 아래/위 | Tilt 최대속도 -/+ 2 |
| A | 즉시 Pan/Tilt STOP |
| B | 현재 Pan/Tilt 조회 |
| X | 미사용 |
| Y | 구형 GUI는 레이저 펄스, 웹은 ON/OFF 토글 |
| LB/RB | 미사용 |
| Back | 레이저 OFF |
| Start | 위치 자동 모니터링 ON/OFF |

스틱을 중앙으로 놓으면 STOP을 한 번 전송합니다. 패드 분리, 게임패드 제어 해제, 프로그램 종료 시에도 STOP을 요청합니다. A로 정지한 뒤에는 스틱이 중앙에 들어온 것을 확인하기 전까지 Jog를 다시 시작하지 않습니다. 이동 완료 또는 타임아웃 때 진동 피드백을 시도하며, 패드나 드라이버가 진동 API를 지원하지 않으면 생략됩니다.

Windows/Linux 드라이버 모드에 따라 raw 축·버튼 번호가 달라질 수 있습니다. 앱이 `SDL 표준`으로 표시되면 A/B/X/Y, LB/RB, Back/Start, D-pad가 OS 차이를 흡수한 내부 번호로 변환됩니다. `raw`로 표시되는 특이 패드는 게임패드 탭의 실시간 `Axes`, `Pressed buttons`, `D-pad hats` 값을 확인한 후 아래 파일 상단의 상수만 수정하면 됩니다.

```text
pt503_tester/gamepad_mapping.py
```

주로 수정하는 항목은 `PAN_AXIS`, `TILT_AXIS`, `INVERT_*`, `HAT_INDEX`, `BUTTON_*`, `STICK_DEADZONE`, `SPEED_CURVE`, `SPEED_STEP`입니다.

### 고급·점검

- 6바이트 입력 시 체크섬 자동 추가
- 7바이트 입력 시 체크섬 검증
- Self-check, 원격 재시작, 공장 초기화 전 확인창
- 제조사 Focus Raw 조회 및 표준 Pelco-D 장치 유형 조회
- 맞춤형 펌웨어용 전원 인가 Self-check ON/OFF

## ACK와 이동 완료

제공 Pelco-D 문서에는 대부분의 명령에 대한 4바이트 General Response가 있지만 이것은 명령 수신/처리 ACK이며 축이 목표점에 도달했다는 신호는 아닙니다. 전용 이동 완료 프레임은 제공 문서에 없습니다.

앱은 절대좌표 이동과 레시피 포인트 이동에서 Pan/Tilt 위치를 백그라운드로 조회하고, 설정한 허용오차 안의 값이 지정 횟수 연속 확인되면 `DONE`을 기록합니다. 제한 시간 안에 확인하지 못하면 `TIMEOUT`을 기록합니다.

## 통신 로그

화면에는 다음 정보가 기록됩니다.

- 밀리초 단위 시간
- TX/RX/ECHO/ACK 해석/DONE/SETTLED/TIMEOUT/SYSTEM/ERROR 상태
- 실제 HEX 데이터
- 프레임 해석
- 조회 응답시간
- 체크섬 결과

자동 JSON Lines 로그 위치:

```text
%LOCALAPPDATA%\OSRND\PT503Tester\logs
```

`CSV 저장` 버튼으로 현재 화면 세션을 Excel 호환 UTF-8 CSV로 내보낼 수 있습니다. `화면 지우기`는 기존 자동 로그 파일을 삭제하지 않습니다.

위치 자동 모니터링과 이동 완료 판정용 주기 조회/응답은 로그에 반복 출력하지 않습니다. 사용자가 누른 `지금 위치 조회`와 고급 탭의 개별 조회는 정상적으로 TX/RX 로그에 남습니다.

## 단일 EXE 만들기

`build_exe.bat`을 실행하면 PyInstaller가 다음 파일을 생성합니다.

```text
dist\PT503_Tester.exe
```

## 테스트

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
pytest -q
ruff check .
```

프로토콜 테스트에는 실제 PT503이 필요하지 않습니다. 실제 장비 시험은 반드시 이동 공간과 비상정지를 확보한 뒤 진행하세요.

### RDK X5 GPIO 레이저 (2026-10-15)

레이저 기본 backend는 `Hobot.GPIO`, BOARD 물리 핀 **31**, HIGH=ON / LOW=OFF입니다.
PTM 서버만 핀을 소유하며 초기화·종료 시 LOW를 출력합니다. `laser.on {}` / `laser.off {}`로 제어하며 ARM/DISARM은 제거했습니다.
웹 레이저 탭은 ON/OFF, 바이패스와 상태별 조사 설정을 제공합니다. 기존 pulse API는 호환용으로 유지하지만 ARM을 요구하지 않습니다.
포인트 이동은 먼저 OFF하고 위치 feedback으로 완료된 뒤 허가된 출력만 ON합니다.
GPIO 라이브러리/핀 설정 실패는 상태의 `laser.error`와 `connected=false`로 보고합니다.
Windows에서는 GPIO를 에뮬레이션하지 않습니다. 관련 테스트는 `tests/test_gpio_laser.py`의 mock을 사용합니다.

RDK의 실제 실행 Python에서 `import Hobot.GPIO`가 가능해야 합니다. 신규 Linux venv는 system site packages를 사용하며,
기존 venv는 필요하면 `python3 -m venv --system-site-packages .venv-linux`로 갱신합니다.
GPIO 모드에서는 Arduino serial 연결을 받지 않습니다. 명시적으로 저장된 `laser.backend=modbus`만 구형 RS485 경로를 유지합니다.

#### 상태별 레이저 조사

각 상태에서 점등(`on`), 점멸(`blink`), 소등(`off`)을 선택하고 점멸 전환 간격을 100~60000 ms로 설정합니다. 예를 들어 500 ms는 ON 500 ms/OFF 500 ms입니다. ‘레이저 설정 저장’은 출력을 새로 켜지 않고 설정만 저장합니다. ‘시험’은 해당 상태를 수동 적용하며 OFF·정지·다음 장치 제어·시간 만료로 종료됩니다.

| 상태/API 값 | 자동 전환 기준 | 기본 방식 |
| --- | --- | --- |
| 포인트 조사 / `point_irradiation` | 허가된 포인트 이동의 실제 완료 | 점등 |
| 체결점 위치 확인 / `position_check` | 유효한 Vision 위치가 기존 허용오차 안에서 안정됨 | 점등 |
| 잘못된 체결점 / `wrong_point` | 유효한 Vision 위치 오차가 기존 허용오차를 초과함 | 점멸, 250 ms 전환 |
| 작업 완료 / `work_complete` | 현재 목표와 일치하는 명령 6의 유효한 완료 결과 수신 | 소등 |

자동 전환에는 최신 Core와 PTM을 함께 적용해야 합니다. 설정은 PTM의 서버 설정 파일 `laser.state_profiles`에 저장됩니다. `laser.state {"state":"wrong_point","manual":true}`로 웹 시험과 같은 동작을 요청하고, 자동 요청은 `manual:false`와 목표 `recipe_id`/`point_id`를 사용합니다. 이동·OFF·정지·바이패스·출력 허가 해제는 점멸 예약도 취소합니다. 작업 완료 신호는 별도 자동 상태 허가를 사용하며, 완료 후 수동 재점등 차단은 유지합니다. 설정된 패턴은 마지막 GPIO 명령 상태이며 광학 출력 검출 결과가 아닙니다.

`laser.power_save {"enabled":true}`는 자동 출력 OFF·예약 ON 취소와 자동 ON 차단을 수행합니다.
명시적인 `laser.on {}`는 자동 OFF/절전 중에도 ON 시점부터 최대 1800초 허용합니다.
다음 이동·Jog·원점·정지 등 장치 제어 또는 시간 만료 시 OFF하고 자동 모드로 돌아갑니다.
상태 조회/화면 전환은 수동 시간을 연장하지 않습니다. Core의 FORCE_STOP/Fault/바이패스는 우선합니다.
절전 명령의 `automatic_off` / `manual_allowed`는 유효 수동 상태 보존과 강제 차단을 구분합니다.
false는 자동 차단만 해제하며 켜지지 않습니다. Core가 30분 PLC 무요청 정책을 관리합니다.
PTM 웹/HTTP/TCP `power_save.wake`는 Core TCP에 전달합니다.
주소 기본값은 127.0.0.1:8000이며 `PT503_CORE_HOST` / `PT503_CORE_PORT` 또는 명령의 host/port로 바꿉니다.
이 환경변수는 PTM 자신의 TCP bind 주소인 `PT503_TCP_HOST/PORT`와 별개입니다.
전체 계약은 Monitor `docs/Tracker_Monitor_구현_지침서.md` 16절에 있습니다.

추가 확정된 ON/OFF 전용 동작과 검출 없는 상태 계약은 같은 지침서 18절에 있습니다.
GPIO 입력/readback은 읽지 않습니다. `on`은 마지막 성공한 GPIO 명령이며 실제 광 방출 검출값이 아닙니다.
상태에 `feedback_available=false`, `on_source=commanded_gpio_level`을 제공하고 armed 필드는 없습니다.
PTM 웹 레이저 바이패스는 레이저 탭에 있습니다. PLC D+66/D+67과 레이저 경고/고장 요약,
대표 코드 5000~5099는 Reserved/0입니다. GPIO 초기화/쓰기 오류는 API 오류·상태 진단·로그로 보고하며
출력 검출 알람을 만들지 않습니다. OFF 쓰기 실패 시 이동을 막고 모터 STOP은 별도로 시도합니다.
