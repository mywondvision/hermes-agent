# Первичная квалификация Linux Codex с синтетической подпиской

Сценарий рассчитан на ту же Vast VM, к которой владелец уже подключается по SSH. Управляющее соединение и интерфейсы хоста остаются в исходной network namespace. Изоляция внешней сети действует на службу E2 и её дочерние процессы. Дополнительная аренда не требуется. Настоящий Linux Codex получает синтетическую политику рабочего пространства по HTTPS и два ответа через поставляемый Responses connector. Первичные входы содержат только измерения поставки и ядра.

## Входы администратора

1. Закрепите ревизию этого checkout и подготовьте тестовое окружение с pytest и cryptography офлайн. В приведённой команде checkout установлен в `/opt/hermes-symphony/e2/source`, Python тестового окружения в `/opt/hermes-symphony/e2/testenv/bin/python`. Нужны user namespaces и cgroup v2 с делегированием. Для этого прогона используйте синтетический образ, пустой профиль и отдельный каталог состояния. Оставьте SSH, firewall и хостовую маршрутизацию в существующей конфигурации. Публичные адреса и credentials хоста в fixture не передаются.
2. Подготовьте принадлежащий root и смонтированный только для чтения образ squashfs в `/opt/hermes-symphony/e2/runtime`. Зафиксируйте хеш самого образа и provenance ELF `/usr/bin/codex` из `rust-v0.158.0`, commit `064c6b8c737f5b41d171fdda80bd9ef10ad06eb3`. Образу требуются зависимости Linux Codex sandbox, bubblewrap, shell, `/usr/bin/python3` и каталог модели с поддерживаемым инструментом `exec_command`. Предусмотрите каталоги для монтирования `/work`, `/proc`, `/dev`, гостевого scratch и пустой файл `/etc/symphony/e2-ca.pem`. Закреплённые проверяемые файлы должны быть обычными файлами с защищёнными родителями. В `/etc/symphony/codex/` допускаются только `config.toml` и несекретный `.symphony-qualification-marker`. Тест генерирует собственные TLS-ключ, CA и фиктивный ChatGPT auth.
3. В `/etc/codex/requirements.toml` внутри образа запишите только `chatgpt_base_url = "https://127.0.0.1:18463/backend-api"`. В `/etc/symphony/codex/config.toml` используйте следующий профиль. Замените значение `model` точным именем модели из закреплённого локального каталога. Сеть и инструменты в VM полностью синтетические.

```toml
model = "<имя из локального каталога>"
model_catalog_json = "/etc/symphony/models.json"
default_permissions = "symphony-provider"
approval_policy = "never"
web_search = "disabled"

[shell_environment_policy]
inherit = "none"

[features]
network_proxy = false
hooks = false
plugins = false
enable_mcp_apps = false
enable_request_compression = false

[permissions.symphony-provider.filesystem]
":root" = "read"
"/work" = "write"
"/tmp" = "write"
"/etc/symphony/codex" = "deny"

[permissions.symphony-provider.network]
enabled = false
```

4. Подготовьте отдельный UID/GID `hermes-symphony` и приватный каталог `/var/lib/hermes-symphony/provider-e2` с режимом 0700 на ограниченной по объёму файловой системе. Из действующей управляющей сессии снимите readback `/proc/self/ns/net` и `/proc/sys/kernel/random/boot_id`. Установите root-owned `/etc/hermes-symphony/provider-e2-input.json`. Каждый хеш ниже — SHA-256 реальных байтов, в нижнем регистре. Проверка откажет при неожиданном ключе, записи в образ или замене закреплённых путей.

| JSON-ключ | Значение |
|---|---|
| `purpose` | `fake-only-provider-e2-v2` |
| `source` | Полный commit Codex, указанный выше |
| `boot` | boot_id этой VM, 32 hex без дефисов |
| `host_net` | Исходный `net:[inode]` VM |
| `image_sha256` | Файл, являющийся backing file смонтированного squashfs |
| `bwrap_sha256` | Гостевой host executable `/usr/bin/bwrap` вне squashfs |
| `codex_sha256`, `python_sha256` | Бинарные файлы внутри образа по указанным выше путям |
| `config_sha256`, `catalog_sha256`, `requirements_sha256` | Реальные три файла конфигурации внутри образа |
| `connector_sha256` | `9c26e3a3577774b6e50fa9c3ee4daaf041f721faca1623de47bf7fa41cac2f8c` для поставляемого `plugins/symphony_pm/transport/linux_responses_connector.py` из закреплённого checkout. Подтверждается канонический root-owned путь загруженного модуля |

## Однократный запуск в подготовленной VM

```sh
systemd-run --unit=hermes-symphony-provider-e2 --collect --pipe --wait \
  --property=Type=exec --property=User=hermes-symphony --property=Group=hermes-symphony \
  --property=Delegate=yes --property=PrivateNetwork=yes --property=NoNewPrivileges=yes \
  --property=CapabilityBoundingSet= --property=AmbientCapabilities= \
  --property=KillMode=control-group --property=MemoryMax=4G --property=TasksMax=256 \
  --property=CPUQuota=100% --property=RuntimeMaxSec=4min \
  --working-directory=/opt/hermes-symphony/e2/source \
  --setenv=HERMES_PYTHON=/opt/hermes-symphony/e2/testenv/bin/python \
  /usr/bin/bash scripts/run_tests.sh -j 1 --file-retries 0 --file-timeout 210 --fail-on-skip \
  tests/plugins/test_symphony_pm_linux_provider_e2.py
```

Имя и cgroup этой службы служат явным включением. Обычный запуск тестов пропускает сценарий. В указанной службе сбой предусловий или нарушение бюджета завершает тест ошибкой. Таймер и KillMode службы ограничивают весь процесс квалификации. Заранее обеспечьте возможность непривилегированного чтения реальных PID/start, ELF и namespace ссылок внутри делегированной группы. Отказ ядра в таком наблюдении оставляет квалификацию открытой.

Пока команда выполняется, сохраните идентификатор запуска из второго терминала командой `systemctl show hermes-symphony-provider-e2.service --property=InvocationID --value`. Для приёмки требуются код выхода 0, два прошедших теста и ноль пропусков. Откройте каталог, в котором `binding.json` содержит этот идентификатор в `binding.service_id`, а `request.nonce` совпадает с именем каталога. Сохраните его свежий `cold-e2.json`. Нативный тест сверяет интервал запроса и остановки с текущим запуском, сопоставляет хеши входов, привязки и записи остановки, затем сравнивает наблюдения с `observations.json`.

## Проверяемый результат

Тест измеряет действующие systemd-ограничения, текущий boot, образ и приватную network namespace службы. В ней доступен только lo и отсутствует внешний маршрут. Процесс получает одноразовое разрешение на запуск после помещения в измеренную leaf cgroup. Внутри этой же группы выполняются оба HTTPS-стенда, обработчик connector, `codex sandbox -P symphony-provider` и `codex exec`. Служба работает без capabilities, с NoNewPrivileges. Проверки снимают факты сети у службы, процесса стенда, клиента и потока connector. Квитанция остановки привязана ко всей leaf cgroup.

Фиктивный GET accounts/check обрабатывает только локальный HTTPS ingress с синтетическими Bearer и account ID. При `NO_CONSTRAINT` он пересылает оба POST без изменений тела в настоящий parser, TLS и потоковый ответ поставляемого connector. В тесте подставляются эфемерный CA и установленный TCP peer по числовому loopback. Обращение к DNS исключено, а connector проверяет сертификат для своего штатного имени. Каждый POST получает собственный readback PID/start, cgroup inode, network namespace, версии TLS и адреса локального peer. Стенд E2 принимает квитанцию только после двух обменов через connector. Используется его обработчик в процессе стенда. Запуск резидентного production-сервиса и проверка его внешнего UDS peer относятся к отдельной приёмке.

Клиент имеет собственный writable tmpfs CODEX_HOME с RO auth и config. Фактический worker проверяет недоступность этого каталога и работающего endpoint, окружение и дескрипторы. Для исходного call_id проверяются успешное завершение команды и её namespace в сравнении с клиентом, найденным по ELF, argv, PID/start и cgroup. Стенды регистрируют каждый входящий GET и POST до проверки маршрута и заголовков. В положительном сценарии индекс успешно обслуженного GET политики должен предшествовать первому из двух POST. Во втором сценарии fake accounts/check сообщает конфликтующий origin. Для приёмки требуются сообщение о конфликте и ноль попыток POST на обоих стендах.

Отдельный офлайн-тест отзывает локальное разрешение, пока relay ждёт первый байт ответа upstream. Он проверяет EOF со стороны upstream, разрыв клиентского соединения и ответ 403 на следующий локальный запрос в пределах таймаута теста.

В `/var/lib/hermes-symphony/provider-e2/<nonce>/` остаются `binding.json`, сырые `observations.json` и `stop`. Файл `cold-e2.json` создаётся после подтверждения исчезновения процессов и опустошения исходной cgroup, сверки inode и удаления именно этой группы. В E2 поле `tool_output_source` положительной квитанции равно `linux-cli`. Фикстура E1 помечает свой ответ `synthetic`. Сохраняйте квитанции вместе с исходной ревизией и измерениями VM.

Этот сценарий проверяет клиентскую политику в сеть-изолированном стенде. Для production требуются отдельный principal клиента, защищённая доставка subscription credential и соблюдение официальной workspace routing policy через доверенный HTTPS-путь. До отдельной приёмки этой топологии normal Linux launcher возвращает `LINUX_PROVIDER_SEPARATE_PRINCIPAL_REQUIRED` до резерва. Квитанция E2 не является настройкой допуска и не подтверждает завершение внешних запросов.
