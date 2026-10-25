# Тестовое окружение ДО импортов backend: только тестовые значения, не секреты.
import os

os.environ.setdefault("JWT_SECRET", "unit_test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("PII_PEPPER", "unit_test_pepper_local_only")
