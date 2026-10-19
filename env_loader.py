# -*- coding: utf-8 -*-
"""
.env 文件加载器 - 兼容 turb 项目

支持额外类型: list_str_multiline (多行字符串列表，代理池常用)
"""
import os
from pathlib import Path


def load_env(override: bool = False) -> None:
    """加载项目根目录的 .env 文件。"""
    from dotenv import load_dotenv
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(str(env_path), override=override)


def env_str(key: str, default: str = "") -> str:
    """读取字符串环境变量，支持 .env。"""
    load_env(override=False)
    return os.environ.get(key, default)


def _parse_multiline_list(raw: str) -> list[str]:
    """把多行字符串解析成列表，去空去重保持顺序。"""
    if not raw:
        return []
    out = []
    for line in raw.replace(";", "\n").replace("|", "\n").splitlines():
        item = line.strip().strip('"\'').strip(",")
        if item and item not in out:
            out.append(item)
    return out


def apply_env_overrides(globals_dict: dict, type_map: dict) -> None:
    """
    用环境变量覆盖模块级常量。

    支持类型:
        str, int, float, bool, list_str_multiline
    """
    for key, type_name in type_map.items():
        env_val = os.environ.get(key)
        if env_val is None:
            continue
        try:
            if type_name == "bool":
                globals_dict[key] = env_val.strip().lower() in ("1", "true", "yes", "on")
            elif type_name == "int":
                globals_dict[key] = int(env_val)
            elif type_name == "float":
                globals_dict[key] = float(env_val)
            elif type_name == "list_str_multiline":
                if env_val.strip() and env_val.strip() not in ('[]', ''):
                    globals_dict[key] = _parse_multiline_list(env_val)
            else:
                globals_dict[key] = env_val
        except (ValueError, TypeError):
            pass
