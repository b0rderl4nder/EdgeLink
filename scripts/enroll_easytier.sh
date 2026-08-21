#!/bin/sh
set -eu

usage() {
    echo "用法：sudo $0 <EasyTier官方tar.gz> <已审核配置.conf> [实例名]" >&2
    exit 2
}

[ "$#" -ge 2 ] && [ "$#" -le 3 ] || usage
[ "$(id -u)" -eq 0 ] || { echo "请使用sudo运行" >&2; exit 1; }

ARCHIVE=$1
CONFIG_SOURCE=$2
INSTANCE=${3:-default}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

case "$INSTANCE" in
    *[!a-zA-Z0-9_-]*|'') echo "实例名不合法" >&2; exit 1 ;;
esac
[ -f "$ARCHIVE" ] || { echo "找不到EasyTier归档" >&2; exit 1; }
[ -f "$CONFIG_SOURCE" ] || { echo "找不到EasyTier配置" >&2; exit 1; }

if tar -tzf "$ARCHIVE" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
    echo "归档包含不安全路径，已拒绝" >&2
    exit 1
fi

STAGE=$(mktemp -d /tmp/edgelink-easytier.XXXXXX)
trap 'rm -rf "$STAGE"' EXIT INT TERM
tar -xzf "$ARCHIVE" -C "$STAGE"
CORE=$(find "$STAGE" -type f -name easytier-core -print -quit)
CLI=$(find "$STAGE" -type f -name easytier-cli -print -quit)
[ -n "$CORE" ] && [ -n "$CLI" ] || {
    echo "归档中缺少easytier-core或easytier-cli" >&2
    exit 1
}

install -d -m 0755 /opt/easytier /opt/easytier/config
if [ -f /opt/easytier/config/"$INSTANCE".conf ]; then
    BACKUP=/opt/easytier/config/"$INSTANCE".conf.backup
    cp -a /opt/easytier/config/"$INSTANCE".conf "$BACKUP"
    chmod 0600 "$BACKUP"
fi
install -m 0755 "$CORE" /opt/easytier/easytier-core
install -m 0755 "$CLI" /opt/easytier/easytier-cli
install -m 0600 "$CONFIG_SOURCE" /opt/easytier/config/"$INSTANCE".conf
install -m 0644 "$SOURCE_DIR/deploy/easytier@.service" /etc/systemd/system/easytier@.service
ln -sfn /opt/easytier/easytier-core /usr/local/sbin/easytier-core
ln -sfn /opt/easytier/easytier-cli /usr/local/sbin/easytier-cli

systemctl daemon-reload
systemctl enable --now "easytier@$INSTANCE.service"
if ! systemctl is-active --quiet "easytier@$INSTANCE.service"; then
    echo "EasyTier服务启动失败；原配置备份仍保留在设备上" >&2
    exit 1
fi
echo "EasyTier实例已注册并启动。配置内容未输出。"

