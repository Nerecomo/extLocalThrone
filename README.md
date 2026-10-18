# Throne Site Adder — host

Локальное приложение-помощник (native messaging host) для браузерного расширения
**«Добавить сайт в Throne»** ([Chrome Web Store](https://chromewebstore.google.com/detail/lnfbcmcfedcjfkgomfmepbcnglmhnmho)).
Расширение передаёт ему домен открытой вкладки, а host:

1. записывает домен **в конец** списка `domain_suffix` правила `proxy` в **активном** профиле маршрутизации
   [Throne](https://github.com/throneproj/Throne) (если такого правила нет, создаёт его; дубликаты не добавляет);
2. перед каждой записью делает резервную копию `throne.db`;
3. применяет изменение через **Throne API**: профиль штатно перезапускается примерно за секунду, без диалогов.

Работает только в Windows.

## Требования

* Windows 10/11.
* [Throne](https://github.com/throneproj/Throne/releases) **1.4.0-beta.2 или новее**, в котором включён Throne API: **Настройки → Integration Settings → Throne API → Enable**. Ключ API host берёт сам из конфига Throne, копировать его не нужно.
* [Python](https://www.python.org/downloads/) **3.8 или новее**. Сторонние пакеты не нужны: достаточно стандартной библиотеки.
* Расширение «Добавить сайт в Throne» в Chrome, Yandex Browser или Edge (ID `lnfbcmcfedcjfkgomfmepbcnglmhnmho`).

## Установка

1. Скачайте `ThroneSiteAdder-host-vX.Y.Z.zip` со страницы **Releases** и распакуйте в любую папку.
2. Запустите двойным кликом **`install.cmd`**. Права администратора не нужны.
3. Перезапустите браузер.

Установщик:

* копирует host в `%LOCALAPPDATA%\ThroneSiteAdder\host`, после этого распакованную папку можно удалить;
* находит Throne: сначала запущенный, затем по записи его установщика, иначе спрашивает путь;
* регистрирует host для Chrome и Yandex, Chromium, Edge и Firefox в `HKCU`;
* делает самопроверку: показывает активный профиль и сообщает, включён ли Throne API.

Параметры `install.cmd` / `install.ps1`:

| Параметр | |
|---|---|
| `-ThroneDir "D:\Apps\Throne"` | Папка Throne, если она не нашлась автоматически |
| `-ExtensionId id1,id2` | Другие ID расширения вместо ID из Chrome Web Store |
| `-RestartMethod api\|kill\|none` | Как применять изменения. `api` — по умолчанию. `kill` — для Throne без API: завершает ядро, как пункт трея «Restart Proxy», и в режиме TUN это может вызвать ошибку адаптера. `none` — только сохранять изменения |

> Запускайте установщик из обычного окна PowerShell или Проводника. Приложения в формате MSIX, например Claude Desktop, Windows изолирует: их запись в `%LOCALAPPDATA%` попадает в виртуальную копию, которую браузер не видит.

## Удаление

Запустите **`uninstall.cmd`**. Скрипт удаляет ключи реестра и папку host. Бэкапы конфига Throne остаются в `%LOCALAPPDATA%\ThroneSiteAdder\backups`; чтобы удалить и их, используйте `uninstall.cmd -RemoveBackups`. Само расширение удаляется в браузере.

## Откат конфига Throne

Журнал `%LOCALAPPDATA%\ThroneSiteAdder\host.log` содержит каждый запрос, каждое изменение и путь к бэкапу.

1. Полностью закройте Throne: трей → Выход.
2. Скопируйте нужный `%LOCALAPPDATA%\ThroneSiteAdder\backups\throne-ГГГГММДД-ЧЧММСС.db` поверх `<папка Throne>\config\throne.db`.
3. Удалите рядом с ним `throne.db-wal` и `throne.db-shm`.
4. Запустите Throne.

## Как это работает

* **Где Throne хранит маршруты.** Throne хранит настройки в `config\throne.db` (SQLite в режиме WAL). Активный профиль задаётся ключом `settings.current_route_id`. Правило proxy — это запись в таблице `route_rules` с `action='route'` и `outbound_id=-1`; в первую очередь выбирается «Simple Address Proxy». Домены попадают в `domain_suffix_json`, `*.example.com` записывается как `.example.com` (только поддомены), IP-адреса и подсети — в `ip_cidr_json`.
* **Запись.** Каждая запись — одна SQLite-транзакция `BEGIN IMMEDIATE`, поэтому база не повреждается, даже если Throne закрыт или падает во время записи. Перед записью host делает онлайн-копию базы через SQLite backup API и хранит последние 30 копий.
* **Throne API.** Клиент протокола [`throne-api/1`](https://github.com/throneproj/Throne-api/blob/main/spec/protocol.md) написан на чистом Python: HMAC-SHA256 и ChaCha20-Poly1305 по RFC 8439. Host делает `GET /v1/status`, затем `PUT /v1/connection {"profile": <запущенный>, "restart": true}`. Ключ, порт и флаг включения он читает из `settings` (`remote_api_*`).
* **Почему домены пишутся в БД, а не через API.** В Throne API пока нет эндпоинта для правил маршрутизации, поэтому через API идёт только перезапуск.

Протокол с расширением — native messaging, по одному JSON-сообщению на запуск:

```
{"cmd": "status"} | {"cmd": "list"} | {"cmd": "add", "value": "example.com"}
{"cmd": "remove", "value": "example.com"} | {"cmd": "import", "values": ["a.com", "10.0.0.0/8"]}
```

## Ограничения

* Пока открыто окно «Маршруты» в Throne, оно держит профили в памяти и при нажатии OK перезапишет изменения host.
* Профили, заданные сырым JSON (raw), host не меняет. Удалённые профили (remote) Throne перезапишет при автообновлении.
* Поддомен уже добавленного домена не добавляется: `api.x.com` уже покрывается записью `x.com`.

## Разработка

```bash
python -m unittest discover -s tests -v
```

Тесты проверяют:
* валидацию ввода;
* правки БД на фикстуре со схемой Throne;
* бэкапы, блокировки и конкурентную запись;
* протокол native messaging;
* клиент Throne API: векторы RFC 8439, официальные векторы `throne-api/1` и сквозные тесты с поддельным сервером.

Релиз собирается так:

```bash
powershell -ExecutionPolicy Bypass -File make-release.ps1
```

Скрипт создаёт `release\ThroneSiteAdder-host-v<версия>.zip` и `.sha256`; версия берётся из `VERSION` в `host/throne_host.py`. На GitHub релиз собирается автоматически, когда вы пушите тег `v<версия>`: см. `.github/workflows/release.yml`.

## Лицензия

MIT, см. [LICENSE](LICENSE). Тест-векторы `tests/fixtures/throne_api_v1.json` взяты из [throneproj/Throne-api](https://github.com/throneproj/Throne-api) (MIT).
