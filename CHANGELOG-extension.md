# Изменения расширения

Релизы расширения помечаются тегами `extension-v<версия>`. Та же версия публикуется в
[Chrome Web Store](https://chromewebstore.google.com/detail/lnfbcmcfedcjfkgomfmepbcnglmhnmho).

## 1.0.1

* На экране «Не найдено host-приложение» появилась ссылка на страницу Releases с host.
* Локальная сборка из этого репозитория получила тот же ID, что в Chrome Web Store (`lnfbcmcfedcjfkgomfmepbcnglmhnmho`), поэтому host принимает её без настройки.

## 1.0.0

Первая версия.

* Поле ввода с корневым доменом открытой вкладки (по Public Suffix List). Список добавленных доменов с кнопкой «Удалить», экспорт и импорт.
* Связь с host через native messaging. Разрешения: только `activeTab` и `nativeMessaging`.
