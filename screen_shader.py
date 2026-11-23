#!/usr/bin/env python3
"""ncm-lite 实验性全屏流质 Shader 视觉叠加层入口（模块化实现位于 shader_overlay/）。"""
from __future__ import annotations

import sys

from shader_overlay import find_gtk_python, launch_screen_shader, main

__all__ = ["find_gtk_python", "launch_screen_shader", "main"]

if __name__ == "__main__":
    sys.exit(main())
