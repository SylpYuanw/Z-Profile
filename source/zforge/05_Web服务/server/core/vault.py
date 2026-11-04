# -*- coding: utf-8 -*-
"""
ZForge / 加密保险库
===================

存 Cookie、账号密码这类东西。**只读文件的人拿不到明文。**

────────────────────────────────────────────────────────────────
为什么自己写而不装 cryptography
────────────────────────────────────────────────────────────────
本机没装 cryptography, 而这个工具要让别人 clone 下来就能跑。
多一个依赖就多一道装不上的门槛。所以用标准库实现:

  密钥派生   PBKDF2-HMAC-SHA256, 迭代 200,000 次
  加密       HMAC-SHA256 作伪随机函数, 计数器模式生成密钥流, 与明文异或
  完整性     HMAC-SHA256 对整个密文做认证 (Encrypt-then-MAC)

这个构造的安全性依据:
  · HMAC-SHA256 是标准 PRF, 计数器模式下输出与随机不可区分 (PRF/CTR 构造)
  · Encrypt-then-MAC 是公认正确的认证加密组合方式
  · 没有任何"自己发明密码学"的部分, 只是把标准原语按标准方式拼起来

**这不是玩具。** 它抵御的是"文件被看到 / 被复制 / 误提交进 git",
而不是"攻击者拿到你的磁盘并离线爆破"。后者需要硬件级密钥, 超出本工具范围。

────────────────────────────────────────────────────────────────
密钥放哪
────────────────────────────────────────────────────────────────
不在项目目录里。放在 %APPDATA%\\ZForge\\ 下, 这样:
  · git status 永远是干净的, 密钥不可能被提交
  · 项目整个发给别人, 别人也解不开你的 Cookie

损失密钥 = 数据解不开, 没有后门, 没有恢复。这是设计意图。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import sys
from pathlib import Path
from typing import Any

# ============================================================
# 参数
# ============================================================
PBKDF2_ROUNDS = 200_000
SALT_BYTES = 16
NONCE_BYTES = 16
KEY_BYTES = 32
_TAG = b"ZFV1"  # 格式版本标记, 以后换算法靠它区分


def config_dir() -> Path:
    """密钥与配置的存放位置 (项目目录之外)。"""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "ZForge"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _machine_entropy() -> bytes:
    """本机指纹 —— 作为密钥派生的一部分, 让密钥绑定这台机器。"""
    parts = [
        os.environ.get("COMPUTERNAME", ""),
        os.environ.get("USERNAME", os.environ.get("USER", "")),
        os.environ.get("PROCESSOR_IDENTIFIER", ""),
        str(Path.home()),
    ]
    return "|".join(parts).encode("utf-8", "replace")


class VaultError(Exception):
    """保险库相关错误。"""


class Vault:
    """
    加密保险库。

    典型用法:
        v = Vault()
        token = v.encrypt({"cookies": [...], "ua": "..."})
        data  = v.decrypt(token)
    """

    def __init__(self, name: str = "main", key_file: Path | None = None) -> None:
        self.name = name
        self.key_file = key_file or (config_dir() / f"vault-{name}.key")
        self.salt_file = config_dir() / f"vault-{name}.salt"
        self._key: bytes | None = None

    # ---------------- 密钥管理 ----------------
    def _load_or_create_salt(self) -> bytes:
        if self.salt_file.exists():
            salt = base64.b64decode(self.salt_file.read_text(encoding="ascii").strip())
            if len(salt) != SALT_BYTES:
                raise VaultError(f"salt 文件损坏: {self.salt_file}")
            return salt
        salt = secrets.token_bytes(SALT_BYTES)
        self.salt_file.write_text(base64.b64encode(salt).decode("ascii"), encoding="ascii")
        try:
            os.chmod(self.salt_file, 0o600)
        except OSError:
            pass
        return salt

    def _load_or_create_master(self) -> bytes:
        if self.key_file.exists():
            raw = base64.b64decode(self.key_file.read_text(encoding="ascii").strip())
            if len(raw) != KEY_BYTES:
                raise VaultError(f"密钥文件损坏: {self.key_file}")
            return raw
        master = secrets.token_bytes(KEY_BYTES)
        self.key_file.write_text(base64.b64encode(master).decode("ascii"), encoding="ascii")
        try:
            os.chmod(self.key_file, 0o600)
        except OSError:
            pass
        return master

    @property
    def key(self) -> bytes:
        """派生出的实际加密密钥 (缓存)。"""
        if self._key is None:
            salt = self._load_or_create_salt()
            master = self._load_or_create_master()
            # 主密钥 + 本机指纹 一起进 KDF, 换台机器解不开
            material = master + _machine_entropy()
            self._key = hashlib.pbkdf2_hmac("sha256", material, salt, PBKDF2_ROUNDS, dklen=KEY_BYTES)
        return self._key

    def rotate_machine_binding(self) -> None:
        """如果换了机器名/用户名导致解不开, 这个方法重建密钥。数据会失效。"""
        self._key = None
        if self.salt_file.exists():
            self.salt_file.unlink()

    # ---------------- 加解密 ----------------
    @staticmethod
    def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
        """HMAC-SHA256 计数器模式生成密钥流。"""
        out = bytearray()
        counter = 0
        while len(out) < length:
            out += hmac.new(key, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
            counter += 1
        return bytes(out[:length])

    @staticmethod
    def _mac(key: bytes, *chunks: bytes) -> bytes:
        m = hmac.new(key, digestmod=hashlib.sha256)
        for c in chunks:
            m.update(len(c).to_bytes(8, "big"))
            m.update(c)
        return m.digest()

    def encrypt(self, obj: Any) -> str:
        """把任意可 JSON 序列化的对象加密成字符串。"""
        plain = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # 加密密钥和认证密钥从主密钥分离派生, 互不干扰
        enc_key = hashlib.sha256(self.key + b"|enc").digest()
        mac_key = hashlib.sha256(self.key + b"|mac").digest()
        nonce = secrets.token_bytes(NONCE_BYTES)
        ct = bytes(a ^ b for a, b in zip(plain, self._keystream(enc_key, nonce, len(plain))))
        tag = self._mac(mac_key, _TAG, nonce, ct)
        blob = _TAG + nonce + tag + ct
        return base64.urlsafe_b64encode(blob).decode("ascii")

    def decrypt(self, token: str) -> Any:
        """解密。密文被篡改会抛 VaultError, 不会返回错误数据。"""
        try:
            blob = base64.urlsafe_b64decode(token.encode("ascii"))
        except Exception as e:
            raise VaultError("不是合法的密文格式") from e

        head = len(_TAG) + NONCE_BYTES + 32
        if len(blob) < head or blob[: len(_TAG)] != _TAG:
            raise VaultError("密文头部不合法或版本不支持")

        nonce = blob[len(_TAG): len(_TAG) + NONCE_BYTES]
        tag = blob[len(_TAG) + NONCE_BYTES: head]
        ct = blob[head:]

        enc_key = hashlib.sha256(self.key + b"|enc").digest()
        mac_key = hashlib.sha256(self.key + b"|mac").digest()

        if not hmac.compare_digest(tag, self._mac(mac_key, _TAG, nonce, ct)):
            raise VaultError(
                "完整性校验失败: 密文被改动过, 或者密钥/机器指纹变了。"
                " 如果你换过电脑或用户名, 需要重新录入账号信息。"
            )

        plain = bytes(a ^ b for a, b in zip(ct, self._keystream(enc_key, nonce, len(ct))))
        return json.loads(plain.decode("utf-8"))

    # ---------------- 便捷方法 ----------------
    def save_to(self, path: Path, obj: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.encrypt(obj), encoding="ascii")

    def load_from(self, path: Path) -> Any:
        if not path.exists():
            return None
        return self.decrypt(path.read_text(encoding="ascii").strip())

    def info(self) -> dict:
        return {
            "name": self.name,
            "key_file": str(self.key_file),
            "salt_file": str(self.salt_file),
            "key_exists": self.key_file.exists(),
            "rounds": PBKDF2_ROUNDS,
            "machine_bound": True,
        }


if __name__ == "__main__":
    # 自测: 往返、篡改检测、错误密钥
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        v = Vault("selftest", key_file=td / "k.key")
        v.salt_file = td / "k.salt"

        payload = {"cookies": [{"name": "_tb_token_", "value": "abc123"}], "ua": "Mozilla/5.0", "n": 42}
        token = v.encrypt(payload)
        assert v.decrypt(token) == payload, "往返失败"
        print("[ok] 加解密往返")

        assert "abc123" not in token, "密文里泄漏了明文"
        print("[ok] 密文中不含明文")

        # 篡改一个字节
        raw = bytearray(base64.urlsafe_b64decode(token.encode()))
        raw[-1] ^= 0x01
        bad = base64.urlsafe_b64encode(bytes(raw)).decode()
        try:
            v.decrypt(bad)
            raise AssertionError("篡改竟然通过了")
        except VaultError:
            print("[ok] 篡改被检出")

        # 换密钥
        v2 = Vault("other", key_file=td / "k2.key")
        v2.salt_file = td / "k2.salt"
        try:
            v2.decrypt(token)
            raise AssertionError("错误密钥竟然解开了")
        except VaultError:
            print("[ok] 错误密钥无法解密")

        # 空对象 / 中文
        assert v.decrypt(v.encrypt({})) == {}
        assert v.decrypt(v.encrypt({"备注": "测试中文 🎯"}))["备注"] == "测试中文 🎯"
        print("[ok] 空对象与中文")

    print("\n[Vault] 全部自测通过")
