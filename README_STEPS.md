# Futures Strategy Sync: готовый запуск

Этот комплект заменяет прямые запросы Apps Script к Binance/Bybit. Биржи блокируют Google Apps Script, поэтому теперь данные считает Python, а Apps Script только записывает готовые строки в таблицу.

## Что внутри

- `apps_script_webhook.gs` - вставить в Google Apps Script. Он принимает готовые вкладки и записывает их в таблицу.
- `sync_futures_to_sheets.py` - Python-скрипт, который берет Binance Futures, считает стратегию и отправляет строки в webhook.
- `requirements.txt` - зависимости Python.
- `.github/workflows/futures-sync.yml` - бесплатный запуск через GitHub Actions каждые 2 часа.
- `run_local_windows.bat` - локальный запуск на Windows, если GitHub Actions тоже будет заблокирован Binance.

## Шаг 1. Apps Script

1. Открой таблицу:
   https://docs.google.com/spreadsheets/d/1PCFuUAColEZgV7Be3gXsNhJoFrv34Ni79yR-_3zuJ5o/edit
2. Extensions -> Apps Script.
3. Создай новый файл `Webhook.gs`.
4. Вставь туда содержимое файла `apps_script_webhook.gs`.
5. В строке:
   `const FUTURES_SYNC_SECRET = 'CHANGE_ME_SECRET_123';`
   замени секрет на свой, например:
   `const FUTURES_SYNC_SECRET = 'futures-sync-2026-PRIVATE';`
6. Нажми Save.
7. Выбери функцию `testWebhook` и нажми Run.
8. Дай разрешения Google.
9. В таблице должна появиться вкладка `FUTURES_SYNC_TEST`.

## Шаг 2. Deploy Web App

1. В Apps Script нажми Deploy -> New deployment.
2. Тип deployment: Web app.
3. Execute as: Me.
4. Who has access: Anyone with the link.
5. Нажми Deploy.
6. Скопируй Web app URL. Это будет `SHEET_WEBHOOK_URL`.

Важно: после каждого изменения `Webhook.gs` нужно делать Deploy -> Manage deployments -> Edit -> New version -> Deploy. Иначе веб-версия останется старой.

## Шаг 3. GitHub

В репозиторий положи файлы так:

```text
requirements.txt
sync_futures_to_sheets.py
.github/workflows/futures-sync.yml
```

Файл `apps_script_webhook.gs` в GitHub класть необязательно, он нужен только для Apps Script.

## Шаг 4. GitHub Secrets

В GitHub открой:
Settings -> Secrets and variables -> Actions -> New repository secret

Создай 2 секрета:

- `SHEET_WEBHOOK_URL` = URL Web App из Apps Script
- `SHEET_SECRET` = тот же секрет, который прописан в `FUTURES_SYNC_SECRET`

## Шаг 5. Первый запуск

1. GitHub -> Actions.
2. Выбери `Futures Strategy Sync`.
3. Нажми `Run workflow`.
4. Жди завершения.
5. Проверь вкладки:
   - `FUT_STRAT`
   - `FUT_STRAT_PF`
   - `FUT_STRAT_SL`
   - `FUT_STRAT_SL_PF`
   - `FUT_STRAT_SLT`
   - `FUT_STRAT_SLT_PF`

Если GitHub Actions успешно прошел, дальше он будет обновлять таблицу каждые 2 часа.

## Если GitHub тоже заблокирован Binance

Тогда запускай бесплатно со своего ПК:

1. Установи Python 3.11+.
2. В папке с файлами выполни:
   `pip install -r requirements.txt`
3. Открой `run_local_windows.bat` и замени:
   - `PASTE_WEB_APP_URL_HERE` на Web App URL
   - `CHANGE_ME_SECRET_123` на твой секрет
4. Запусти `run_local_windows.bat`.
5. Если таблица обновилась, добавь этот `.bat` в Windows Task Scheduler раз в 2 часа.

## Настройки

В GitHub workflow можно менять:

```yaml
FUTURES_TOP_N: "30"
FUTURES_HISTORY_DAYS: "90"
FUTURES_INTERVAL: "15m"
```

Если выполнение долгое, поставь `FUTURES_TOP_N: "15"`. Если все работает стабильно, верни `30`.
