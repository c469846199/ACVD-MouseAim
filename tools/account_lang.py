"""tools/account_lang.py — 解密 Xenia Account 文件，查看/修改语言字段

加密方案（free60 Profile_Account）：
    文件 = HMAC-SHA1(16B) + confounder(8B) + account_data(380B)
    retail key = E1BC159C73B1EAE9AB3170F3AD47EBF3
    rc4_key = HMAC-SHA1(key=retail_key, msg=file[0:16])[:16]
    data = RC4(rc4_key, file[0x10:])

语言：解密数据偏移 0x30 = CachedFlags，bit 25-29 = language
用法：
    python tools/account_lang.py read                 # 解密查看
    python tools/account_lang.py set <lang> <country> # 修改并重加密
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys

# Xenia 存档目录因安装位置而异，按以下顺序找：
#   1. 环境变量 XENIA_CONTENT
#   2. 默认 Xenia 文档目录（%USERPROFILE%\Documents\Xenia\content\...）
#   3. 命令行第一个参数
# 找不到就报明确错误，不要让用户对着神秘路径猜。
def _default_account() -> str:
    import glob
    base = os.environ.get("XENIA_CONTENT") or os.path.join(
        os.path.expanduser("~"), "Documents", "Xenia", "content")
    hits = glob.glob(os.path.join(base, "*", "FFFE07D1", "*", "Account"))
    return hits[0] if hits else ""


ACCOUNT = _default_account()
RETAIL_KEY = bytes.fromhex("E1BC159C73B1EAE9AB3170F3AD47EBF3")
DEVKIT_KEY = bytes.fromhex("DAB69AD98E28764F977EE2487E4F3F68")

LANGUAGES = {1: "English", 2: "Japanese", 8: "Traditional Chinese",
             17: "Simplified Chinese"}


def rc4(key: bytes, data: bytes) -> bytes:
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray()
    i = j = 0
    for c in data:
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out.append(c ^ S[(S[i] + S[j]) & 0xFF])
    return bytes(out)


def decrypt(blob: bytes, key: bytes):
    """返回 (confounder, data, ok)"""
    file_hash = blob[:16]
    rc4_key = hmac.new(key, blob[:16], hashlib.sha1).digest()[:16]
    plain = rc4(rc4_key, blob[16:])
    confounder, data = plain[:8], plain[8:]
    check = hmac.new(key, plain, hashlib.sha1).digest()[:16]
    return confounder, data, check == file_hash


def encrypt(data: bytes, key: bytes, confounder: bytes | None = None) -> bytes:
    if confounder is None:
        confounder = os.urandom(8)
    plain = confounder + data
    file_hash = hmac.new(key, plain, hashlib.sha1).digest()[:16]
    rc4_key = hmac.new(key, file_hash, hashlib.sha1).digest()[:16]
    enc = rc4(rc4_key, plain)
    return file_hash + enc


def decode_account(data: bytes):
    reserved = int.from_bytes(data[0:4], "little")
    live = int.from_bytes(data[4:8], "little")
    gamertag = data[8:40].decode("utf-16-le", "replace").rstrip("\x00")
    cached = int.from_bytes(data[0x30:0x34], "little")
    country = (cached >> 8) & 0xFF
    lang = (cached >> 25) & 0x1F
    tier = (cached >> 16) & 0xF
    return reserved, live, gamertag, cached, country, lang, tier


def apply_lang_country(cached: int, lang: int, country: int) -> int:
    cached = (cached & ~(0x1F << 25)) | ((lang & 0x1F) << 25)
    cached = (cached & ~(0xFF << 8)) | ((country & 0xFF) << 8)
    return cached


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "read"
    blob = open(ACCOUNT, "rb").read()
    print("Account: %d 字节" % len(blob))

    conf, data = None, None
    for tag, key in (("retail", RETAIL_KEY), ("devkit", DEVKIT_KEY)):
        c, d, ok = decrypt(blob, key)
        print("[%s key] HMAC 校验: %s" % (tag, "通过 ✓" if ok else "失败 ✗"))
        if ok:
            conf, data = c, d
            break

    if data is None:
        print("两种密钥都解不开")
        return 1

    reserved, live, gamertag, cached, country, lang, tier = decode_account(data)
    print()
    print("=== 解密成功（%s key）===" % tag)
    print("  Reserved : 0x%08X" % reserved)
    print("  LiveFlags: 0x%08X" % live)
    print("  Gamertag : %r" % gamertag)
    print("  CachedFlags: 0x%08X" % cached)
    print("  Country  : %d" % country)
    print("  Language : %d (%s)" % (lang, LANGUAGES.get(lang, "未知")))

    if cmd == "set":
        new_lang = int(sys.argv[2])
        new_country = int(sys.argv[3]) if len(sys.argv) > 3 else country
        data = bytearray(data)
        new_cached = apply_lang_country(cached, new_lang, new_country)
        data[0x30:0x34] = new_cached.to_bytes(4, "little")
        newblob = encrypt(bytes(data), RETAIL_KEY, conf)
        os.replace(ACCOUNT, ACCOUNT + ".bak_enc")
        open(ACCOUNT, "wb").write(newblob)
        print()
        print("✓ 已写入: language=%d (%s), country=%d" %
              (new_lang, LANGUAGES.get(new_lang, "?"), new_country))
        print("  原文件备份: %s.bak_enc" % ACCOUNT)
        print("  下次启动 Xenia → 游戏应读取新语言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
