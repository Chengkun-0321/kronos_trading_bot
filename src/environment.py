"""共用根目錄環境設定；程序已設定的值優先。"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_env(path):
    """讀取Path為字串dict；不展開變數或執行shell，缺檔回傳空dict。"""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.removeprefix('export ').partition('=')
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', key):
            raise ValueError('環境檔格式不符（未輸出內容）')
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError('環境檔引號不成對')
            value = value[1:-1]
        else:
            value = value.split(' #', 1)[0].rstrip()
        values[key] = value
    return values


def load_env():
    """載入根目錄.env至程序環境；保留外部明示值，不回傳機密。"""
    for key, value in read_env(ROOT / '.env').items():
        os.environ.setdefault(key, value)
