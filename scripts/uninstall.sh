#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "请使用 sudo 运行卸载程序" >&2
    exit 1
fi
systemctl disable --now edgelink-agent.service 2>/dev/null || true
rm -f /etc/systemd/system/edgelink-agent.service
rm -f /etc/NetworkManager/dispatcher.d/90-edgelink-agent
rm -f /usr/local/bin/edgelinkctl
rm -rf /opt/edgelink-agent
systemctl daemon-reload
echo "程序已卸载；/etc/edgelink 中的配置和密钥已保留。"
