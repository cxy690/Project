# -*- coding: utf-8 -*-
"""临时：启动 6006 -> 8001 转发并验证（用完即删）。"""
from __future__ import annotations

import paramiko

PY = "/root/autodl-tmp/education/.venvs/app/bin/python"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("connect.nmb1.seetacloud.com", port=16565, username="root",
          password="OhtrexH67upK", timeout=25)

_, out, err = c.exec_command(
    f"(setsid {PY} /root/natapp/fwd.py 6006 8001 > /tmp/fwd6006.log 2>&1 &); sleep 3; "
    "echo -n 'local6006='; curl -s -o /dev/null -m 5 -w '%{http_code}\\n' http://127.0.0.1:6006/app/login/; "
    "echo '--- log ---'; cat /tmp/fwd6006.log",
    timeout=45,
)
print(out.read().decode("utf-8", "ignore"))
print(err.read().decode("utf-8", "ignore")[:300])
c.close()
