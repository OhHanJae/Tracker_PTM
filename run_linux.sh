#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

usage() {
  cat <<'EOF'
Usage:
  ./run_linux.sh                 # Headless Web + TCP server on current PC IP
  ./run_linux.sh --port 9000     # Web port override, TCP stays 8765
  ./run_linux.sh --tcp-port 9001 # TCP port override
  ./run_linux.sh client          # PySide6 TCP API client only
  ./run_linux.sh legacy-gui      # Optional old direct-control GUI mode

Environment:
  PT503_HOST=0.0.0.0             # Web bind host (all interfaces)
  PT503_PORT=8080                # Web port
  PT503_TCP_HOST=0.0.0.0         # TCP bind host (all interfaces)
  PT503_TCP_PORT=8765            # TCP JSON Lines port

Before using USB-RS485 on Ubuntu, the current user usually needs dialout:
  sudo usermod -aG dialout "$USER"
Then log out and log back in.
EOF
}

warn_if_gui_packages_missing() {
  if ! command -v dpkg >/dev/null 2>&1; then
    return
  fi

  local missing=()
  for pkg in libxcb-cursor0 libxkbcommon-x11-0 libxcb-xinerama0 libegl1; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
      missing+=("$pkg")
    fi
  done

  if ((${#missing[@]})); then
    echo "[WARN] GUI 실행에 필요한 Ubuntu 패키지가 없을 수 있습니다:"
    printf '       %s\n' "${missing[@]}"
    echo "       설치 예: sudo apt update && sudo apt install ${missing[*]}"
  fi
}

warn_if_serial_permission_missing() {
  if groups "${USER:-$(id -un)}" | grep -qw dialout; then
    return
  fi
  echo "[WARN] 현재 사용자가 dialout 그룹에 없습니다."
  echo "       USB-RS485 포트 권한 오류가 나면 다음 실행 후 재로그인하세요:"
  echo '       sudo usermod -aG dialout "$USER"'
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "${1:-}" == "help" ]]; then
  usage
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] python3 명령을 찾을 수 없습니다."
  echo "        Ubuntu 24.04에서는 보통 sudo apt install python3 python3-venv 로 설치합니다."
  exit 1
fi

DEFAULT_VENV=".venv-linux"
VENV_DIR="${PT503_VENV:-$DEFAULT_VENV}"
VENV_PYTHON="$VENV_DIR/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
  if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    echo "[ERROR] Python 3.10 or later is required."
    exit 1
  fi
  echo "[1/3] Creating virtual environment..."
  if ! python3 -m venv "$VENV_DIR"; then
    echo "[ERROR] 가상환경 생성 실패. sudo apt install python3-venv 를 확인하세요."
    exit 1
  fi
fi

if ! "$VENV_PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "[ERROR] $VENV_DIR must use Python 3.10 or later."
  exit 1
fi

install_from_wheelhouse_or_pip() {
  local requirements_file="$1"
  if [[ -d "wheels" ]] && compgen -G "wheels/*.whl" >/dev/null; then
    "$VENV_PYTHON" -m pip install --no-index --find-links wheels -r "$requirements_file"
    return
  fi
  "$VENV_PYTHON" -m pip install --retries 1 --timeout 10 -r "$requirements_file"
}

explain_offline_server_dependency() {
  cat <<'EOF'
[ERROR] pyserial을 찾을 수 없습니다.
        이 장비가 인터넷/DNS가 안 되는 상태라 pip 설치가 실패한 것으로 보입니다.

        Ubuntu 패키지로 설치 가능한 환경이면:
          sudo apt update
          sudo apt install python3-serial
          ./run_linux.sh

        완전 오프라인 환경이면 인터넷 되는 PC에서 pyserial wheel을 받아
        이 프로젝트의 wheels/ 폴더에 넣은 뒤 다시 실행하세요:
          mkdir -p wheels
          # 예: pyserial-3.5-py2.py3-none-any.whl 파일을 wheels/에 복사
          ./run_linux.sh
EOF
}

ensure_headless_dependencies() {
  if "$VENV_PYTHON" -c "import serial" >/dev/null 2>&1; then
    return 0
  fi
  if python3 -c "import serial" >/dev/null 2>&1; then
    echo "[INFO] 시스템 python3의 pyserial을 사용합니다."
    VENV_PYTHON="$(command -v python3)"
    return 0
  fi
  if install_from_wheelhouse_or_pip requirements-server.txt; then
    return 0
  fi
  explain_offline_server_dependency
  return 1
}

parse_effective_server_args() {
  local next_key=""
  TCP_ENABLED=1
  for arg in "$@"; do
    if [[ "$next_key" == "port" ]]; then
      WEB_PORT="$arg"
      next_key=""
      continue
    fi
    if [[ "$next_key" == "tcp-port" ]]; then
      TCP_PORT="$arg"
      next_key=""
      continue
    fi
    case "$arg" in
      --port)
        next_key="port"
        ;;
      --port=*)
        WEB_PORT="${arg#*=}"
        ;;
      --tcp-port)
        next_key="tcp-port"
        ;;
      --tcp-port=*)
        TCP_PORT="${arg#*=}"
        ;;
      --no-tcp)
        TCP_ENABLED=0
        ;;
    esac
  done
}

port_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn "sport = :$port" 2>/dev/null | awk 'NR > 1 { found = 1 } END { exit found ? 0 : 1 }'
    return
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    return
  fi
  python3 - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind(("0.0.0.0", port))
except OSError:
    raise SystemExit(0)
finally:
    sock.close()
raise SystemExit(1)
PY
}

print_port_owner() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp "sport = :$port" 2>/dev/null || true
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN || true
  elif command -v fuser >/dev/null 2>&1; then
    fuser -v "${port}/tcp" || true
  fi
}

ensure_port_free() {
  local label="$1"
  local port="$2"
  if ! port_in_use "$port"; then
    return 0
  fi
  echo "[ERROR] $label port $port is already in use."
  echo "        A previous PT503 server is probably still running."
  echo
  print_port_owner "$port"
  echo
  echo "Options:"
  echo "  1) Use the already running server: http://$ip_hint:$WEB_PORT"
  echo "  2) Stop the old process after checking its PID: kill <PID>"
  echo "  3) Start on another port: ./run_linux.sh --port 9000 --tcp-port 9001"
  exit 98
}

mode="${1:-server}"
if [[ "$mode" == "client" ]]; then
  if (($#)); then
    shift
  fi
  warn_if_gui_packages_missing
  echo "[2/3] Checking client dependencies..."
  if ! "$VENV_PYTHON" -c "import PySide6, serial, pygame" >/dev/null 2>&1; then
    install_from_wheelhouse_or_pip requirements.txt
  fi
  echo "[3/3] Starting PT503 PySide6 API client..."
  exec "$VENV_PYTHON" main.py --client "$@"
fi

if [[ "$mode" == "legacy-gui" || "$mode" == "gui" ]]; then
  if (($#)); then
    shift
  fi
  warn_if_gui_packages_missing
  warn_if_serial_permission_missing
  echo "[2/3] Checking legacy GUI dependencies..."
  if ! "$VENV_PYTHON" -c "import PySide6, serial, pygame" >/dev/null 2>&1; then
    install_from_wheelhouse_or_pip requirements.txt
  fi
  echo "[3/3] Starting PT503 legacy direct-control GUI..."
  exec "$VENV_PYTHON" main.py --legacy-gui "$@"
fi

if [[ "$mode" == "server" || "$mode" == "headless" || "$mode" == "--server" ]]; then
  if (($#)); then
    shift
  fi
fi

WEB_HOST="${PT503_HOST:-0.0.0.0}"
WEB_PORT="${PT503_PORT:-8080}"
TCP_HOST="${PT503_TCP_HOST:-$WEB_HOST}"
TCP_PORT="${PT503_TCP_PORT:-8765}"
extra_args=("$@")
parse_effective_server_args "${extra_args[@]}"
args=(--server --host "$WEB_HOST" --port "$WEB_PORT" --tcp-host "$TCP_HOST" --tcp-port "$TCP_PORT" "${extra_args[@]}")

echo "[2/3] Checking headless dependencies..."
ensure_headless_dependencies
warn_if_serial_permission_missing
ip_hint="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
ip_hint="${ip_hint:-<ubuntu-ip>}"
if [[ "$TCP_ENABLED" == "1" && "$WEB_PORT" == "$TCP_PORT" ]]; then
  echo "[ERROR] Web port and TCP port cannot be the same: $WEB_PORT"
  echo "        Example: ./run_linux.sh --port 9000 --tcp-port 9001"
  exit 98
fi
ensure_port_free "Web" "$WEB_PORT"
if [[ "$TCP_ENABLED" == "1" ]]; then
  ensure_port_free "TCP" "$TCP_PORT"
fi
echo "[3/3] Starting PT503 headless servers..."
echo "      Web: http://$ip_hint:$WEB_PORT"
if [[ "$TCP_ENABLED" == "1" ]]; then
  echo "      TCP: $ip_hint:$TCP_PORT"
else
  echo "      TCP: disabled"
fi
exec "$VENV_PYTHON" main.py "${args[@]}"
