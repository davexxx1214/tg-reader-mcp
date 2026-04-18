"""
Telegram 登录脚本 — 首次使用时运行，生成 tg_session.session 文件。

用法:
    python login.py

凭证从 config.yaml 的 telegram_api 段读取，不要在代码中硬编码。
"""

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("❌ 缺少 pyyaml，请先安装: pip install pyyaml")
    sys.exit(1)

from telethon import TelegramClient

CONFIG_FILE = Path(__file__).resolve().parent / "config.yaml"

if not CONFIG_FILE.exists():
    print(f"❌ 配置文件不存在: {CONFIG_FILE}")
    print("   请先复制模板并填入真实凭证:")
    print("   cp config.example.yaml config.yaml")
    sys.exit(1)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

tg_api = config.get("telegram_api", {})
api_id = tg_api.get("api_id", 0)
api_hash = tg_api.get("api_hash", "")

if not api_id or not api_hash or api_hash.startswith("your_"):
    print("❌ Telegram API 凭证未配置，请在 config.yaml 的 telegram_api 段填入真实值")
    print("   申请地址: https://my.telegram.org/apps")
    sys.exit(1)

client = TelegramClient("tg_session", api_id, api_hash)
client.start()
print("✅ 登录成功，tg_session.session 已创建")
