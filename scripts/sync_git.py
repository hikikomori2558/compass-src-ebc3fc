#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Безопасная синхронизация репозитория (git pull --rebase origin main)
"""

import subprocess
import sys

def main():
    print("=== Синхронизация rapira-clinical-compass с GitHub ===")
    subprocess.run(["git", "status", "-s"])
    res = subprocess.run(["git", "pull", "--rebase", "origin", "main"])
    if res.returncode != 0:
        print("[!] Сначала закоммить свои локальные изменения в GitHub Desktop, затем повтори Pull.")
        sys.exit(res.returncode)
    print("[OK] Код успешно обновлен!")

if __name__ == "__main__":
    main()
