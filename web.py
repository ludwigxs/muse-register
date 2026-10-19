# -*- coding: utf-8 -*-
"""
Muse by Meta 注册机 - WebUI 入口

启动: python web.py [--host 127.0.0.1] [--port 8799] [--debug]
浏览器打开: http://127.0.0.1:8799
"""
import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    parser = argparse.ArgumentParser(description="Muse by Meta 注册机 WebUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    from webui.app import run
    run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
