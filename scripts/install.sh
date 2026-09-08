#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "请使用 sudo 运行安装程序" >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
CONFIG_SOURCE=${EDGELINK_CONFIG_SOURCE:-$SOURCE_DIR/config.example.json}
INSTALL_ROOT=/opt/edgelink-agent
RELEASES_DIR=$INSTALL_ROOT/releases
CONFIG_DIR=/etc/edgelink
DISPATCHER=/etc/NetworkManager/dispatcher.d/90-edgelink-agent
START_MODE=${EDGELINK_START:-auto}

# Updating a network controller must never silently activate it on a device
# where another controller already owns the recovery path.  Existing active
# deployments continue to receive normal upgrades; a first installation stays
# inert until the operator explicitly starts it after its preflight record is
# reviewed.  EDGELINK_START=0 is an install-only operation and deliberately
# does not stop an already active service.
case "$START_MODE" in
    0|1|auto) ;;
    *)
        echo "EDGELINK_START只能是auto、0或1。" >&2
        exit 1
        ;;
esac
SERVICE_WAS_ENABLED=false
SERVICE_WAS_ACTIVE=false
if systemctl is-enabled --quiet edgelink-agent.service 2>/dev/null; then
    SERVICE_WAS_ENABLED=true
fi
if systemctl is-active --quiet edgelink-agent.service 2>/dev/null; then
    SERVICE_WAS_ACTIVE=true
fi

for command_name in python3 nmcli systemctl curl getent gdbus busctl; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "缺少依赖：$command_name" >&2
        exit 1
    fi
done

if [ ! -f "$CONFIG_SOURCE" ]; then
    echo "找不到配置模板：$CONFIG_SOURCE" >&2
    exit 1
fi

install -d -m 0755 "$RELEASES_DIR" "$CONFIG_DIR"
RELEASE_ID=$(date -u +%Y%m%dT%H%M%SZ)-$$
STAGE_DIR=$RELEASES_DIR/.stage-$RELEASE_ID
RELEASE_DIR=$RELEASES_DIR/$RELEASE_ID
install -d -m 0755 "$STAGE_DIR"
trap 'rm -rf "$STAGE_DIR"' EXIT INT TERM
cp -a "$SOURCE_DIR/edgelink_agent" "$STAGE_DIR/edgelink_agent"
install -m 0755 "$SOURCE_DIR/deploy/run_agent.py" "$STAGE_DIR/run_agent.py"
if [ -f "$SOURCE_DIR/BUILD_INFO.json" ]; then
    install -m 0644 "$SOURCE_DIR/BUILD_INFO.json" "$STAGE_DIR/BUILD_INFO.json"
fi
chown -R root:root "$STAGE_DIR"
find "$STAGE_DIR" -type d -exec chmod 0755 {} +
find "$STAGE_DIR" -type f -exec chmod 0644 {} +
chmod 0755 "$STAGE_DIR/run_agent.py"

CONFIG_REPLACED=false
CONFIG_HAD_PREVIOUS=false
if [ ! -f "$CONFIG_DIR/config.json" ] || [ "${EDGELINK_REPLACE_CONFIG:-0}" = "1" ]; then
    if [ -f "$CONFIG_DIR/config.json" ]; then
        install -m 0600 "$CONFIG_DIR/config.json" "$CONFIG_DIR/config.json.previous"
        CONFIG_HAD_PREVIOUS=true
    fi
    install -m 0644 "$CONFIG_SOURCE" "$CONFIG_DIR/config.json"
    CONFIG_REPLACED=true
fi

if [ ! -f "$CONFIG_DIR/secrets.env" ]; then
    if [ -n "${EDGELINK_AP_PASSWORD_FILE:-}" ]; then
        if [ ! -f "$EDGELINK_AP_PASSWORD_FILE" ]; then
            echo "找不到EDGELINK_AP_PASSWORD_FILE指定的文件。" >&2
            exit 1
        fi
        EDGELINK_AP_PASSWORD=$(sed -n '1p' "$EDGELINK_AP_PASSWORD_FILE")
    elif [ -z "${EDGELINK_AP_PASSWORD:-}" ]; then
        EDGELINK_AP_PASSWORD=12345678
    fi
    export EDGELINK_AP_PASSWORD
    python3 - <<'PY'
import os, re
value = os.environ.get("EDGELINK_AP_PASSWORD", "")
valid = bool(re.fullmatch(r"[0-9A-Fa-f]{64}", value)) or bool(
    re.fullmatch(r"[A-Za-z0-9._~@%+=:-]{8,63}", value)
)
if not valid:
    raise SystemExit("EDGELINK_AP_PASSWORD不符合WPA密码规范")
PY
    umask 077
    printf 'EDGELINK_AP_PASSWORD=%s\n' "$EDGELINK_AP_PASSWORD" > "$CONFIG_DIR/secrets.env"
    echo "已保存配网热点密码；安装日志不会显示密码。"
fi
if [ "$(wc -l < "$CONFIG_DIR/secrets.env" | tr -d ' ')" -ne 1 ] || \
   ! LC_ALL=C grep -Eq '^EDGELINK_AP_PASSWORD=([0-9A-Fa-f]{64}|[A-Za-z0-9._~@%+=:-]{8,63})$' \
       "$CONFIG_DIR/secrets.env"; then
    echo "$CONFIG_DIR/secrets.env 格式无效；为避免热点无法连接，安装已停止。" >&2
    exit 1
fi
chmod 0600 "$CONFIG_DIR/secrets.env"

install -m 0644 "$SOURCE_DIR/deploy/edgelink-agent.service" /etc/systemd/system/edgelink-agent.service
install -m 0755 "$SOURCE_DIR/deploy/90-edgelink-agent" "$DISPATCHER"
if ! PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGE_DIR" python3 -m edgelink_agent.preflight \
    --config "$CONFIG_DIR/config.json"; then
    if [ "$CONFIG_REPLACED" = true ] && [ "$CONFIG_HAD_PREVIOUS" = true ]; then
        install -m 0644 "$CONFIG_DIR/config.json.previous" "$CONFIG_DIR/config.json"
    fi
    echo "部署前检查未通过，未切换EdgeLink版本。请修复上方错误后重新安装。" >&2
    exit 1
fi

mv "$STAGE_DIR" "$RELEASE_DIR"
trap - EXIT INT TERM
OLD_TARGET=$(readlink "$INSTALL_ROOT/current" 2>/dev/null || true)
ln -s "$RELEASE_DIR" "$INSTALL_ROOT/current.new"
mv -Tf "$INSTALL_ROOT/current.new" "$INSTALL_ROOT/current"

cat > /usr/local/bin/edgelinkctl <<EOF
#!/bin/sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$INSTALL_ROOT/current \
    exec /usr/bin/python3 -m edgelink_agent.cli "\$@"
EOF
chmod 0755 /usr/local/bin/edgelinkctl
install -m 0755 "$SOURCE_DIR/scripts/set_ap_password.sh" \
    /usr/local/sbin/edgelink-set-ap-password

systemctl daemon-reload
STARTED=false
SHOULD_START=false
case "$START_MODE" in
    1) SHOULD_START=true ;;
    auto)
        if [ "$SERVICE_WAS_ENABLED" = true ] || [ "$SERVICE_WAS_ACTIVE" = true ]; then
            SHOULD_START=true
        fi
        ;;
esac
if [ "$SHOULD_START" = true ]; then
    systemctl enable edgelink-agent.service
    systemctl reset-failed edgelink-agent.service 2>/dev/null || true
    if systemctl restart edgelink-agent.service; then
        attempt=0
        while [ "$attempt" -lt 20 ]; do
            if curl --silent --fail --max-time 2 http://127.0.0.1:8787/healthz >/dev/null; then
                STARTED=true
                break
            fi
            attempt=$((attempt + 1))
            sleep 1
        done
    fi
    if [ "$STARTED" != true ]; then
        if [ "$CONFIG_REPLACED" = true ] && [ "$CONFIG_HAD_PREVIOUS" = true ]; then
            install -m 0644 "$CONFIG_DIR/config.json.previous" "$CONFIG_DIR/config.json"
        fi
        if [ -n "$OLD_TARGET" ]; then
            ln -s "$OLD_TARGET" "$INSTALL_ROOT/current.rollback"
            mv -Tf "$INSTALL_ROOT/current.rollback" "$INSTALL_ROOT/current"
            systemctl reset-failed edgelink-agent.service 2>/dev/null || true
            systemctl restart edgelink-agent.service || true
        else
            rm -f "$INSTALL_ROOT/current"
            systemctl disable --now edgelink-agent.service 2>/dev/null || true
        fi
        rm -rf -- "$RELEASE_DIR"
        echo "新版本未在20秒内通过健康检查，已尝试恢复上一版。" >&2
        exit 1
    fi
fi

find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
    | sort -nr \
    | awk 'NR > 3 {sub(/^[^ ]+ /, ""); print}' \
    | while IFS= read -r obsolete_release; do
        case "$obsolete_release" in
            "$RELEASES_DIR"/*) rm -rf -- "$obsolete_release" ;;
        esac
    done
if [ "$SHOULD_START" = true ]; then
    echo "EdgeLink Agent 已安装并通过健康检查。运行 edgelinkctl status 检查状态。"
else
    echo "EdgeLink Agent 已安全安装但未自动启动；先运行 sudo edgelinkctl preflight，确认后使用 EDGELINK_START=1 重新安装或手动启用服务。"
fi
