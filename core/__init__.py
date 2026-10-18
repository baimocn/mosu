"""
墨溯 core —— 核心引擎

本目录既当脚本目录（python core/decrypt_db.py）又当包（from core.exporter import ...）使用，
所以这里把自身路径写进 sys.path，保证包内部模块间的绝对导入始终可用。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
