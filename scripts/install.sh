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

for command_name in python3 nmcli systemctl curl; do
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
RELEASE_ID=$(date -u +%Y%m%dT%H%M%SZ)
STAGE_DIR=$RELEASES_DIR/.stage-$RELEASE_ID-$$
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

if [ ! -f "$CONFIG_DIR/config.json" ]; then
    install -m 0644 "$CONFIG_SOURCE" "$CONFIG_DIR/config.json"
fi

if [ ! -f "$CONFIG_DIR/secrets.env" ]; then
    AP_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')
    umask 077
    printf 'EDGELINK_AP_PASSWORD=%s\n' "$AP_PASSWORD" > "$CONFIG_DIR/secrets.env"
    echo "已生成配网热点密码，请从 $CONFIG_DIR/secrets.env 安全读取。"
fi
chmod 0600 "$CONFIG_DIR/secrets.env"

install -m 0644 "$SOURCE_DIR/deploy/edgelink-agent.service" /etc/systemd/system/edgelink-agent.service
install -m 0755 "$SOURCE_DIR/deploy/90-edgelink-agent" "$DISPATCHER"
if ! PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGE_DIR" python3 -m edgelink_agent.preflight \
    --config "$CONFIG_DIR/config.json"; then
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

systemctl daemon-reload
systemctl enable edgelink-agent.service
STARTED=false
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
    if [ -n "$OLD_TARGET" ]; then
        ln -s "$OLD_TARGET" "$INSTALL_ROOT/current.rollback"
        mv -Tf "$INSTALL_ROOT/current.rollback" "$INSTALL_ROOT/current"
        systemctl restart edgelink-agent.service || true
    fi
    echo "新版本未在20秒内通过健康检查，已尝试恢复上一版。" >&2
    exit 1
fi

find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
    | sort -nr \
    | awk 'NR > 3 {sub(/^[^ ]+ /, ""); print}' \
    | while IFS= read -r obsolete_release; do
        case "$obsolete_release" in
            "$RELEASES_DIR"/*) rm -rf -- "$obsolete_release" ;;
        esac
    done
echo "EdgeLink Agent 已安装。运行 edgelinkctl status 检查状态。"
