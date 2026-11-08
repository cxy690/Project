#!/usr/bin/env bash
# 阿里云 ECS 接入 AutoDL 一键脚本（幂等，可重复执行）
#   浏览器 -> ECS nginx :80 :443 -> SSH 隧道 127.0.0.1:18000 -> AutoDL 127.0.0.1:8001
# 用法：在 ECS 上是 root 执行 bash bootstrap_ecs.sh
# 注意：本脚本不安装应用本体，应用仍跑在 AutoDL 上
set -u

ADL_HOST="connect.nmb1.seetacloud.com"
ADL_PORT="16565"
ADL_USER="root"
ADL_PASS="OhtrexH67upK"
ADL_APP_PORT="8001"
LOCAL_PORT="18000"
KEY="/root/.ssh/autodl_ed25519"
PASSFILE="/root/.ssh/.autodl_pass"

say() { printf '\n=== %s ===\n' "$1"; }
ok()  { printf '  [ok] %s\n' "$1"; }
bad() { printf '  [!!] %s\n' "$1"; }

say "0/6 系统信息"
. /etc/os-release 2>/dev/null && echo "  $PRETTY_NAME"
PM=""
command -v dnf >/dev/null 2>&1 && PM="dnf"
[ -z "$PM" ] && command -v yum >/dev/null 2>&1 && PM="yum"
[ -z "$PM" ] && command -v apt-get >/dev/null 2>&1 && PM="apt"
echo "  包管理器: ${PM:-未知}"

install_pkg() {
    case "$PM" in
        apt) DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" >/dev/null 2>&1 ;;
        dnf|yum) $PM install -y "$@" >/dev/null 2>&1 ;;
    esac
}

say "1/6 sshd（决定外网能否连入）"
systemctl enable --now sshd >/dev/null 2>&1 || systemctl enable --now ssh >/dev/null 2>&1 || true
ssh-keygen -A >/dev/null 2>&1 || true
systemctl restart sshd >/dev/null 2>&1 || systemctl restart ssh >/dev/null 2>&1 || true
sleep 1
if ss -lntp 2>/dev/null | grep -q ':22 '; then
    ok "22 端口已监听"
else
    bad "22 端口仍未监听 —— 尝试安装 openssh-server"
    if [ "$PM" = "apt" ]; then install_pkg openssh-server; else install_pkg openssh-server; fi
    ssh-keygen -A >/dev/null 2>&1 || true
    systemctl enable --now sshd >/dev/null 2>&1 || systemctl enable --now ssh >/dev/null 2>&1 || true
    sleep 1
    ss -lntp 2>/dev/null | grep -q ':22 ' && ok "22 端口已监听（补装后）" || bad "仍失败，看: systemctl status sshd --no-pager"
fi

say "2/6 关闭本机防火墙（安全组已在上游放行）"
systemctl disable --now firewalld >/dev/null 2>&1 && ok "firewalld 已关闭" || echo "  firewalld 未启用"
ufw disable >/dev/null 2>&1 && ok "ufw 已关闭" || true

say "3/6 安装 nginx / ssh 客户端 / sshpass"
if command -v nginx >/dev/null 2>&1; then
    ok "nginx 已存在: $(nginx -v 2>&1)"
else
    install_pkg nginx
    command -v nginx >/dev/null 2>&1 && ok "nginx 安装完成" || bad "nginx 安装失败，请检查软件源"
fi
if [ "$PM" = "apt" ]; then
    install_pkg openssh-client sshpass
else
    install_pkg openssh-clients sshpass
    command -v sshpass >/dev/null 2>&1 || { install_pkg epel-release; install_pkg sshpass; }
fi
command -v sshpass >/dev/null 2>&1 && ok "sshpass 可用" || bad "sshpass 缺失，隧道将走密钥/交互认证"
command -v ssh >/dev/null 2>&1 && ok "ssh 客户端可用" || bad "ssh 客户端缺失"

say "4/6 生成密钥并写入 AutoDL 授权"
mkdir -p /root/.ssh && chmod 700 /root/.ssh
[ -f "$KEY" ] || ssh-keygen -t ed25519 -N '' -f "$KEY" -q
printf '%s' "$ADL_PASS" > "$PASSFILE" && chmod 600 "$PASSFILE"
if command -v sshpass >/dev/null 2>&1; then
    if sshpass -f "$PASSFILE" ssh-copy-id -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -i "${KEY}.pub" -p "$ADL_PORT" "${ADL_USER}@${ADL_HOST}" >/dev/null 2>&1; then
        ok "公钥已写入 AutoDL"
    else
        bad "公钥写入失败（SSH 隧道仍可用密码方式连接）"
    fi
fi

say "5/6 安装常驻隧道（连接参数外置到 /etc/autodl/conn.env）"
mkdir -p /etc/autodl && chmod 700 /etc/autodl
cat > /etc/autodl/conn.env <<EOF
# AutoDL 连接参数 —— AutoDL 实例重启后改这里，或执行 autodl-reconnect
ADL_HOST=${ADL_HOST}
ADL_PORT=${ADL_PORT}
ADL_USER=${ADL_USER}
ADL_PASS=${ADL_PASS}
ADL_APP_PORT=${ADL_APP_PORT}
LOCAL_PORT=${LOCAL_PORT}
EOF
chmod 600 /etc/autodl/conn.env

cat > /usr/local/bin/autodl_tunnel.sh <<'EOF'
#!/bin/bash
[ -f /etc/autodl/conn.env ] || { echo "缺少 /etc/autodl/conn.env"; exit 1; }
set -a; . /etc/autodl/conn.env; set +a
printf '%s' "$ADL_PASS" > /root/.ssh/.autodl_pass
chmod 600 /root/.ssh/.autodl_pass
while true; do
    sshpass -f /root/.ssh/.autodl_pass ssh -N \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -o ConnectTimeout=15 -o ExitOnForwardFailure=yes \
        -i /root/.ssh/autodl_ed25519 \
        -p "$ADL_PORT" -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${ADL_APP_PORT}" \
        "${ADL_USER}@${ADL_HOST}"
    echo "[tunnel] 断开，5 秒后重连 $(date '+%F %T')"
    sleep 5
done
EOF
chmod +x /usr/local/bin/autodl_tunnel.sh

cat > /usr/local/bin/autodl-reconnect <<'EOF'
#!/bin/bash
# 用法: autodl-reconnect <端口> <密码> [主机]
PORT="${1:-}"; PASS="${2:-}"; HOST="${3:-}"
[ -z "$PORT" ] || [ -z "$PASS" ] && { echo "用法: autodl-reconnect <端口> <密码> [主机]"; exit 1; }
E=/etc/autodl/conn.env
OLD_HOST=$(grep '^ADL_HOST=' "$E" | cut -d= -f2-)
HOST="${HOST:-$OLD_HOST}"
sed -i "s|^ADL_HOST=.*|ADL_HOST=${HOST}|; s|^ADL_PORT=.*|ADL_PORT=${PORT}|; s|^ADL_PASS=.*|ADL_PASS=${PASS}|" "$E"
chmod 600 "$E"
systemctl restart autodl-tunnel
sleep 6
CODE=$(curl -s -o /dev/null -m 8 -w '%{http_code}' "http://127.0.0.1:$(grep '^LOCAL_PORT=' "$E" | cut -d= -f2-)/app/login/")
echo "隧道状态: HTTP $CODE"
[ "$CODE" = "200" ] && echo "已恢复" || { echo "未通，检查: journalctl -u autodl-tunnel -n 30 --no-pager"; exit 1; }
EOF
chmod +x /usr/local/bin/autodl-reconnect

cat > /etc/systemd/system/autodl-tunnel.service <<'EOF'
[Unit]
Description=AutoDL app SSH tunnel (127.0.0.1:18000 -> 127.0.0.1:8001)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/autodl_tunnel.sh
Restart=always
RestartSec=5
KillMode=process

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable autodl-tunnel >/dev/null 2>&1
systemctl restart autodl-tunnel
sleep 6
if curl -s -o /dev/null -m 8 -w '%{http_code}' "http://127.0.0.1:${LOCAL_PORT}/app/login/" | grep -qE '^(200|30[0-9])$'; then
    ok "隧道通了，AutoDL 应用可达"
else
    bad "隧道未通，检查: systemctl status autodl-tunnel --no-pager"
fi

say "6/6 配置 nginx 反向代理"
cat > /etc/nginx/conf.d/00-upgrade-map.conf <<'EOF'
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
EOF

cat > /etc/nginx/conf.d/edu.conf <<EOF
server {
    listen 80 default_server;
    server_name _;
    client_max_body_size 200m;
    proxy_connect_timeout 10s;
    proxy_send_timeout 600s;
    proxy_read_timeout 600s;
    proxy_buffering off;

    location / {
        proxy_pass http://127.0.0.1:${LOCAL_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade           \$http_upgrade;
        proxy_set_header Connection        \$connection_upgrade;
    }
}
EOF

rm -f /etc/nginx/conf.d/default.conf
[ -f /etc/nginx/nginx.conf ] && sed -i 's/listen[[:space:]]\+80[[:space:]]\+default_server;/listen 80;/' /etc/nginx/nginx.conf

# 自签 HTTPS：浏览器会提示"不安全"，点继续即可；麦克风等 API 必须有 HTTPS
mkdir -p /etc/nginx/ssl
if [ ! -f /etc/nginx/ssl/edu.crt ]; then
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
        -keyout /etc/nginx/ssl/edu.key -out /etc/nginx/ssl/edu.crt \
        -subj "/C=CN/O=EduAI/CN=118.178.227.23" \
        -addext "subjectAltName=IP:118.178.227.23" >/dev/null 2>&1
fi

cat > /etc/nginx/conf.d/01-edu-ssl.conf <<EOF
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name _;

    ssl_certificate     /etc/nginx/ssl/edu.crt;
    ssl_certificate_key /etc/nginx/ssl/edu.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;

    client_max_body_size 200m;
    proxy_connect_timeout 10s;
    proxy_send_timeout 600s;
    proxy_read_timeout 600s;
    proxy_buffering off;

    location / {
        proxy_pass http://127.0.0.1:${LOCAL_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade           \$http_upgrade;
        proxy_set_header Connection        \$connection_upgrade;
    }
}
EOF

if nginx -t >/dev/null 2>&1; then
    systemctl enable --now nginx >/dev/null 2>&1
    systemctl reload nginx >/dev/null 2>&1
    ok "nginx 已加载配置"
else
    bad "nginx 配置校验失败:"
    nginx -t
fi

say "自检"
sleep 1
echo -n "  隧道直连  : "; curl -s -o /dev/null -m 8 -w '%{http_code}\n' "http://127.0.0.1:${LOCAL_PORT}/app/login/"
echo -n "  nginx 反代: "; curl -s -o /dev/null -m 8 -w '%{http_code}\n' "http://127.0.0.1/app/login/"
echo "  监听端口:"; ss -lntp 2>/dev/null | grep -E ':(22|80|443|18000) ' || echo "    (无)"

echo
echo "完成。外网验证：http://118.178.227.23/app/login/"
echo "若外网不通，检查安全组入方向是否放行 80/443。"
