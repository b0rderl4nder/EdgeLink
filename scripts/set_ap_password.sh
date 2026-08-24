#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "请使用sudo运行" >&2
    exit 1
fi
if [ ! -t 0 ]; then
    echo "热点密码只能在设备本地交互终端中修改" >&2
    exit 1
fi

SECRETS_FILE=/etc/edgelink/secrets.env
SECRETS_DIR=/etc/edgelink
TEMP_FILE=
BACKUP_FILE=

restore_terminal() {
    stty echo 2>/dev/null || true
    if [ -n "$TEMP_FILE" ]; then
        rm -f -- "$TEMP_FILE"
    fi
    if [ -n "$BACKUP_FILE" ]; then
        rm -f -- "$BACKUP_FILE"
    fi
}
trap restore_terminal EXIT INT TERM

printf '请输入新的配网热点密码：' >&2
stty -echo
IFS= read -r FIRST_PASSWORD
printf '\n请再次输入：' >&2
IFS= read -r SECOND_PASSWORD
stty echo
printf '\n' >&2

if [ "$FIRST_PASSWORD" != "$SECOND_PASSWORD" ]; then
    echo "两次输入不一致，未修改。" >&2
    exit 1
fi
export EDGELINK_AP_PASSWORD="$FIRST_PASSWORD"
python3 - <<'PY'
import os
import re

value = os.environ.get("EDGELINK_AP_PASSWORD", "")
valid = bool(re.fullmatch(r"[0-9A-Fa-f]{64}", value)) or bool(
    re.fullmatch(r"[A-Za-z0-9._~@%+=:-]{8,63}", value)
)
if not valid:
    raise SystemExit("密码须为8至63位安全字符，或64位十六进制PSK")
PY

install -d -m 0755 "$SECRETS_DIR"
HAD_SECRET=false
if [ -f "$SECRETS_FILE" ]; then
    BACKUP_FILE=$(mktemp "$SECRETS_DIR/.secrets.env.backup.XXXXXX")
    cp -a "$SECRETS_FILE" "$BACKUP_FILE"
    chmod 0600 "$BACKUP_FILE"
    HAD_SECRET=true
fi
TEMP_FILE=$(mktemp "$SECRETS_DIR/.secrets.env.XXXXXX")
chmod 0600 "$TEMP_FILE"
printf 'EDGELINK_AP_PASSWORD=%s\n' "$FIRST_PASSWORD" > "$TEMP_FILE"
chown root:root "$TEMP_FILE"
mv -f "$TEMP_FILE" "$SECRETS_FILE"
TEMP_FILE=
unset FIRST_PASSWORD SECOND_PASSWORD EDGELINK_AP_PASSWORD
if ! systemctl restart edgelink-agent.service; then
    if [ "$HAD_SECRET" = true ]; then
        mv -f "$BACKUP_FILE" "$SECRETS_FILE"
        BACKUP_FILE=
    else
        rm -f -- "$SECRETS_FILE"
    fi
    systemctl restart edgelink-agent.service 2>/dev/null || true
    echo "服务未能使用新密码启动，已恢复原配置。" >&2
    exit 1
fi
if [ -n "$BACKUP_FILE" ]; then
    rm -f -- "$BACKUP_FILE"
fi
BACKUP_FILE=
echo "热点密码已更新；下次进入配网模式时生效。"
