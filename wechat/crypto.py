"""微信回调的签名校验与消息解密。

微信（公众号 / 企业微信）回调有两种模式：

* **明文模式**：只校验 ``signature``（sha1 排序拼接），消息体是 XML 明文；
* **安全模式**：多一个 ``msg_signature``，消息体里的 ``Encrypt`` 需要用
  ``EncodingAESKey`` 做 AES-256-CBC 解密，解密后还要校验尾部 ``corp_id``。

解密依赖 ``cryptography``（项目里 pandas / paramiko 之外未强制安装）。
**缺依赖时不会让服务崩**：:func:`decrypt_message` 抛出带安装提示的
``WeChatCryptoError``，路由层降级为「只支持明文模式」。
"""
from __future__ import annotations

import base64
import hashlib
import struct
from typing import Iterable

#: 微信 AES 解密后的消息块大小
_BLOCK_SIZE = 32


class WeChatCryptoError(RuntimeError):
    """签名 / 解密失败（消息不该被信任）。"""


def _sha1_of(items: Iterable[str]) -> str:
    raw = "".join(sorted(str(i) for i in items))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def verify_signature(token: str, timestamp: str, nonce: str, signature: str) -> bool:
    """明文模式签名校验（微信文档：sha1(sort(token, timestamp, nonce))）。"""
    if not token or not signature:
        return False
    expected = _sha1_of((token, timestamp, nonce))
    return _const_time_equal(expected, signature)


def verify_msg_signature(
    token: str, timestamp: str, nonce: str, encrypt: str, msg_signature: str
) -> bool:
    """安全模式签名校验（多拼一个密文）。"""
    if not token or not msg_signature or not encrypt:
        return False
    expected = _sha1_of((token, timestamp, nonce, encrypt))
    return _const_time_equal(expected, msg_signature)


def sign_message(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """计算安全模式的消息签名（回复报文也要带）。"""
    return _sha1_of((token, timestamp, nonce, encrypt))


def _const_time_equal(left: str, right: str) -> bool:
    """定长比较，避免签名校验被时间侧信道探测。"""
    a = str(left or "").encode("utf-8")
    b = str(right or "").encode("utf-8")
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= x ^ y
    return diff == 0


def _aes_key(encoding_aes_key: str) -> bytes:
    key = str(encoding_aes_key or "").strip()
    if len(key) != 43:
        raise WeChatCryptoError("WECHAT_ENCODING_AES_KEY 必须是 43 位")
    try:
        return base64.b64decode(key + "=")
    except Exception as err:  # noqa: BLE001
        raise WeChatCryptoError(f"EncodingAESKey 解析失败：{err}") from err


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise WeChatCryptoError("密文为空")
    pad = data[-1]
    if pad < 1 or pad > _BLOCK_SIZE or pad > len(data):
        raise WeChatCryptoError("填充非法")
    return data[:-pad]


def decrypt_message(
    encoding_aes_key: str, encrypt_b64: str, expected_receiver: str = ""
) -> str:
    """解密 ``Encrypt`` 字段，返回明文 XML。

    明文结构：``random(16) + msg_len(4, big-endian) + msg + receiver_id``。
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception as err:  # noqa: BLE001
        raise WeChatCryptoError(
            "安全模式需要 cryptography：pip install cryptography（或在微信后台改用明文模式）"
        ) from err

    key = _aes_key(encoding_aes_key)
    try:
        cipher_text = base64.b64decode(encrypt_b64)
    except Exception as err:  # noqa: BLE001
        raise WeChatCryptoError(f"密文 base64 解析失败：{err}") from err
    if not cipher_text or len(cipher_text) % 16:
        raise WeChatCryptoError("密文长度非法")

    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    plain = _pkcs7_unpad(decryptor.update(cipher_text) + decryptor.finalize())
    if len(plain) < 20:
        raise WeChatCryptoError("解密结果过短")

    msg_len = struct.unpack(">I", plain[16:20])[0]
    if msg_len <= 0 or 20 + msg_len > len(plain):
        raise WeChatCryptoError("消息长度字段非法")
    message = plain[20:20 + msg_len].decode("utf-8", "replace")
    receiver = plain[20 + msg_len:].decode("utf-8", "replace").strip()
    if expected_receiver and receiver and receiver != expected_receiver:
        raise WeChatCryptoError(f"接收方不匹配：{receiver} != {expected_receiver}")
    return message


def encrypt_message(
    encoding_aes_key: str, text: str, receiver: str = "", random16: bytes | None = None
) -> str:
    """安全模式回复：把明文 XML 加密成 ``Encrypt`` 字段。

    结构：``random(16) + msg_len(4) + msg + receiver_id``，PKCS7 填充后 AES-CBC。
    """
    import os
    import struct

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception as err:  # noqa: BLE001
        raise WeChatCryptoError(
            "安全模式需要 cryptography：pip install cryptography（或在微信后台改用明文模式）"
        ) from err

    key = _aes_key(encoding_aes_key)
    body = text.encode("utf-8")
    payload = (
        (random16 or os.urandom(16))
        + struct.pack(">I", len(body))
        + body
        + receiver.encode("utf-8")
    )
    pad = _BLOCK_SIZE - (len(payload) % _BLOCK_SIZE)
    payload += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    cipher_text = encryptor.update(payload) + encryptor.finalize()
    return base64.b64encode(cipher_text).decode("utf-8")
