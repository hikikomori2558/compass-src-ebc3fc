# Боевой VPS для COMPASS (домен projectc.work.gd)

## Требования к VPS (проверено нагрузочным прогоном: 401 rps боевого пути, p95 68 мс)
| Параметр | Рекомендуется | Минимум |
|---|---|---|
| vCPU | 2 (x86_64) | 1 |
| RAM | 4 GB | 2 GB |
| Диск | 40 GB NVMe | 20 GB |
| ОС | Ubuntu 22.04/24.04 или Debian 12 (root) | — |
| Сеть | публичный IPv4, порты 80/443 открыты | — |
| Регион | любой с пингом РФ ≤ 100 мс (RU, AMS, HEL, FRA) | — |
| Провайдеры | Timeweb / Beget / VDSina (карта РФ) или Hetzner (€4.51) | — |

Стек в docker compose: postgres + redis + api (C-ядро собирается в образе) + ЭМК + Caddy(TLS).
Пиковое потребление ≈ 1.5–2 GB RAM. Несколько жюри одновременно — легко (5×10⁴ rps запаса).

## Что делает bootstrap.sh (запускаю я по SSH, либо вы сами):
1. apt: docker + compose
2. rsync репо (без .venv/node_modules)
3. .env: генерирует JWT_SECRET/PII_PEPPER/DEMO_ACCESS_KEY/пароль БД
4. docker compose build && up -d (внутри: схема БД + сид + Caddy сам получит LE-сертификат
   для projectc.work.gd, как только DNS A → IP VPS)
5. Восстановление данных: pg_restore из последнего бэкапа (105 пациентов, вся история)
6. Проверки: healthz, 40 сценариев, боевой тест БД, нагрузочный прогон на VPS-железе

## DNS (freedns, панель projectc.work.gd)
- A: projectc.work.gd → IP нового VPS (Edit у существующей записи)
- CNAME: www.projectc.work.gd → projectc.work.gd (оставить)
- TTL на время переезда: 00:20
