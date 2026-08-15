"""Generate config.example.yaml with secrets redacted (do not commit real config.yaml)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "config.yaml"
DST = ROOT / "config.example.yaml"

SECRET_KEYS = {
    "api_key",
    "api_secret",
    "api_token",
    "blockbeats_api_key",
    "bscscan_api_key",
    "etherscan_api_key",
    "glassnode_api_key",
    "secret",
    "password",
    "token",
    "access_token",
    "private_key",
}

PLACEHOLDERS = {
    "api_secret": "YOUR_API_SECRET",
    "api_token": "YOUR_WEB_TOKEN",
    "token": "YOUR_TOKEN",
    "access_token": "YOUR_TOKEN",
    "private_key": "YOUR_PRIVATE_KEY",
    "password": "YOUR_PASSWORD",
}


def redact_line(line: str) -> str:
    m = re.match(r"^(\s*)([A-Za-z0-9_]+)(\s*:\s*)(.*)$", line)
    if not m:
        return line
    indent, key, sep, rest = m.groups()
    key_l = key.lower()
    val = rest.strip()
    is_secret = (
        key_l in SECRET_KEYS
        or key_l.endswith("_key")
        or key_l.endswith("_secret")
        or key_l.endswith("_token")
        or key_l.endswith("_password")
    )
    if not is_secret:
        return line
    if val in ("", "''", '""', "~", "null", "[]", "{}", "YOUR_BINANCE_API_KEY", "YOUR_BINANCE_API_SECRET"):
        return line
    # quoted empty
    if val in ("''", '""'):
        return line
    placeholder = PLACEHOLDERS.get(key_l, "YOUR_API_KEY")
    # preserve quote style if original was quoted
    if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
        q = val[0]
        return f"{indent}{key}{sep}{q}{placeholder}{q}\n"
    return f"{indent}{key}{sep}{placeholder}\n"


def main() -> None:
    text = SRC.read_text(encoding="utf-8")
    out = "".join(redact_line(line if line.endswith("\n") else line + "\n") for line in text.splitlines())
    header = (
        "# 示例配置（可提交）。复制为 config.yaml 后填写真实密钥。\n"
        "# cp config.example.yaml config.yaml\n"
        "# 推荐敏感项改用 .env（见 .env.example），勿把真实 config.yaml 推到 GitHub。\n\n"
    )
    if not out.lstrip().startswith("# 示例配置"):
        out = header + out
    DST.write_text(out, encoding="utf-8")
    # safety checks
    bad = []
    for needle in ("sk-", "bbp_", "sk-ws-", "EFECQTT", "5349b10c-aa11"):
        if needle.lower() in out.lower() or needle in out:
            bad.append(needle)
    print(f"wrote {DST} ({DST.stat().st_size} bytes)")
    if bad:
        print("WARNING residual patterns:", bad)
    else:
        print("secret scan: ok")


if __name__ == "__main__":
    main()
