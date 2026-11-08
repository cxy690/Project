# -*- coding: utf-8 -*-
"""临时：从实例内部找公网映射地址线索（用完即删）。"""
from __future__ import annotations

import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("connect.nmb1.seetacloud.com", port=16565, username="root",
          password="OhtrexH67upK", timeout=25)

CMD = (
    "echo '=== motd ==='; head -c 800 /etc/autodl-motd 2>/dev/null; echo; "
    "echo '=== init ==='; head -c 600 /etc/autodl-init 2>/dev/null; echo; "
    "echo '=== env ==='; env | grep -iE 'autodl|instance|region|host' | head -10; "
    "echo '=== hosts ==='; cat /etc/hosts; "
    "echo '=== hostname -f ==='; hostname -f 2>/dev/null; hostname; "
    "echo '=== 元数据探测 ==='; timeout 2 curl -s -m 2 http://169.254.169.254/latest/meta-data/ 2>/dev/null | head -c 200; echo"
)
_, out, err = c.exec_command(CMD, timeout=60)
print(out.read().decode("utf-8", "ignore"))
print(err.read().decode("utf-8", "ignore")[:200])
c.close()
