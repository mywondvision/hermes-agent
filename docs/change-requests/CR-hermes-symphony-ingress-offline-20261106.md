# CR: Hermes → Symphony ingress, офлайн-контракт и восстановление cancel

context_hygiene: PASS rev=2b81bd93c646 report=docs/audits/CR-hermes-symphony-ingress-offline-20261106/hygiene-2b81bd93c646-corrected.json

ID: CR-hermes-symphony-ingress-offline-20261106
Статус: кандидат на утверждение; реализация новой дельты не разрешена до explicit `ok`.
Линия: продолжение ADD-HERMES-PM-INGRESS-01 и CR-HERMES-MIGRATION-01; прежние документы не переписываются.
Профиль: Standard / R2 (граница доверия, идентичность повторяемого эффекта, durable state).
Доказательства: E0 сейчас; E1/E2 только для изолированной синтетической реализации после утверждения. Live E3/E4 вне допуска.

## Цель и исходная граница

Закрыть пробел «один cancel на вызов» → «повторная доставка после потери ответа не делает второй native cancel» проверяемым контрактом единственного scheduler owner. Существующие адаптер, `wire()`/`unwire()` и frozen S2 сохраняются. S2 — consumer, не scheduler. `SymphonyPmPort` — Python-интерфейс, не транспорт.

Область: подготовить ingress-контракт и офлайн-проверки устойчивой идентичности операции, одного writer, durable dedupe, fencing, независимого terminal proof и UNKNOWN → сверка без повторного native cancel.

Сохранять существующие файлы и чужие изменения; без reset/clean/stash. Не создавать ключи и постоянный доступ, не подключать production, не делать merge или публикацию без отдельного допуска. Изменения Symphony вне текущего worktree требуют отдельного каталога записи и объёма. Новые модельные сессии и среды автоматически не создавать.

Утверждённый live ingress, его authentication и endpoint в проверенном локальном охвате не установлены. Это блокирует live-транспорт; офлайн-контракт не объявляется существующим API. Фактические пути, HEAD, источники и снимок writer-проверки находятся в локальном `docs/audits/CR-hermes-symphony-ingress-offline-20261106/source-inventory.json`.

## Решение, предлагаемое к утверждению

Следующая реализационная дельта ограничена новым офлайн reference owner и port bridge в test fixtures. Она использует реальный SQLite-файл в task-owned временном каталоге и синтетический effect sink/observer, чтобы проверять restart/reopen и транзакционные границы. Это исполняемая модель контракта для будущего Symphony owner, не новый production scheduler внутри Hermes и не разрешение на его установку. Никакой автозагрузки из runtime.

Не реализовать сетевой HTTP/stdio протокол до установления receiving contract и разрешённого контура. Офлайн bridge реализует существующий `SymphonyPmPort`; его можно передать в `wire()` только в изолированном PluginManager тесте. Plugin-кандидат этим не принимается архитектурно и пользователю не включается.

## Контракт операции (предложение)

1. **Два разных ключа.** Intake owner выдаёт стабильный request ID вне model payload. Durable dedupe key = `(scheduler_namespace, project_ref, request_id)`. Запись связывает operation, canonical payload digest, approved scope revision, contract digest и scheduler-owned canonical job/revision. Повтор с тем же ключом/полным содержимым читает сохранённый результат/текущее неопределённое состояние; несовместимый повтор — CONFLICT до эффекта. Новая epoch, restart, timeout и новый транспортный delivery ID не меняют identity операции. Hermes не создаёт dedupe ledger.
2. **Единственный cancel effect.** Отдельный уникальный effect key связывает scheduler-owned `(job_ref, job_revision, attempt_ref)` и действие cancel. Новое имя запроса, reason или owner epoch не создаёт новый cancel ticket для того же attempt. Одной job identity недостаточно: target фиксирует exact native binding (request digest, attempt, controller epoch, runtime session/process/start identity). Пользователь не выбирает attempt/process/epoch.
3. **Durable admission до эффекта.** Writer атомарно сохраняет operation, target/effect key и `DISPATCH_INTENT` до обращения к effect sink. Уникальные ограничения и compare-and-swap дают только одному claimant право dispatch. После commit ticket необратимо потрачен для dispatch; любой повтор только читает/сверяет. SQLite reference использует явные транзакции и `synchronous=FULL`; process-reopen proof не называется power-loss/storage qualification.
4. **Один writer и fence.** Собственник `(owner_id, monotonically increasing epoch, expiry)` хранится в owner store; каждое изменение проверяет актуальный token транзакционно. Второй живой claimant отвергается. Истечение lease само по себе не доказывает остановку прежнего writer. Takeover закрывает admission старой epoch; effect receiver обязан атомарно отвергать устаревший token, а независимый readback подтверждать exact-attempt revoke/fence до cancel. Если receiver этого не умеет или readback отсутствует — HOLD, effect не вызывается. Файл lock и наличие одного процесса не заменяют этот контракт.
5. **Неопределённый исход.** `RECORDED → DISPATCH_INTENT → CANCEL_REQUESTED / UNKNOWN → PROVEN` — монотонное сохранение знания, а не очередь retry. Ошибка/timeout/потерянный ответ после `DISPATCH_INTENT`, включая crash до фактического effect call, оставляет UNKNOWN/HOLD. На recovery не повторять native cancel, не сбрасывать ticket, не выделять новую попытку. Цена безопасности — возможный UNKNOWN без выполненной отмены; это явно допустимая потеря liveness, требующая сверки/решения владельца, не скрытый success.
6. **Сверка без эффекта.** Owner запрашивает только read-only exact-target observer/effect journal по сохранённым identity. Не найдено/неполно/противоречиво — UNKNOWN/HOLD. Даже `not found` не разрешает повторно потратить cancel ticket. `get_status` может отражать результат такой owner-сверки; в model tool не добавляются `retry`, `resume`, `reconcile`, `approve` или native операции.
7. **Независимое доказательство.** PROVEN требует независимого trusted observer, разрешённого producer, evidence ref, exact target/attempt/epoch/native start binding, свежести и причинного времени после cancel/fence. Отдельно устанавливаются native terminal, descendants stopped, external effects accounted и billing settled. Receipt/worker flags не превращаются в trusted claims. Отсутствие любого требуемого proof сохраняет HOLD; неподтверждённый worker stop не заявляется. На PM-границе действуют существующие dataclasses и verifier без ослабления.
8. **PM receipt и удержание.** Офлайн bridge проецирует owner-знание в существующие `PmSubmissionReceipt`, `PmStatusReceipt`, `PmCancelReceipt`. UNKNOWN не маскируется как успешный cancel; `CANCEL_REQUESTED` не означает stopped. Trusted context создаёт отдельный caller fixture, а не transport из полученного receipt. Резервация остаётся POSSIBLY_CONSUMED_HELD; освобождение, refund, удаление worktree/state и tracker writes не реализуются.

Нельзя обещать exactly-once доставку/исполнение для произвольного native backend: нет общей атомарной транзакции БД и внешнего эффекта. Целевое свойство — максимум один dispatch по durable effect ticket плюс честный UNKNOWN и независимая сверка. Production-достижимость требует квалификации storage, effect receiver fencing и observer отдельно.

## Точный allowlist следующей офлайн-реализации

Все пути относительно текущего Hermes worktree; только после утверждения этой ревизии:

- `tests/fixtures/symphony_pm_ingress/__init__.py` и `owner.py`: SQLite reference owner, synthetic fenced effect sink, read-only observer; используются только тестами.
- `tests/plugins/test_symphony_pm_ingress_offline.py`: real PluginManager/registry/dispatch → `wire()` → reference port; при необходимости synthetic owner → фактический frozen S2 через существующий isolated loader.
- `docs/change-requests/CR-hermes-symphony-ingress-offline-20261106.md`: только отдельно утверждённые изменения требований; результаты не дописываются в спецификацию.
- `docs/audits/CR-hermes-symphony-ingress-offline-20261106/` и `.artifacts/context-hygiene/CR-hermes-symphony-ingress-offline-20261106/`: redacted evidence, review, receipts.

Существующие `tools/symphony_pm_adapter.py`, его тесты и три файла plugin-кандидата сохраняются без изменений. Если новая проверка выявит нужду в production diff или дополнительном пути, сначала bounded delta decision. Старые untracked `tests/tools/symphony_pm_adapter.py`, `tests/tools/tools.zip` и frozen snapshot не трогать. Ни один путь kit/Symphony не входит в write allowlist.

## Dependency impact scan

Потребители: три typed port метода адаптера, out-of-band `TrustedPmContextV1`, plugin registration generations, existing isolated S2 loader и verifier. Production toolset/schema/config не меняются. Shared PRIMARY request/result schemas и runbook остаются frozen. Новый SQL store существует только в test fixture; его формат не объявляется production API или migration target.

## Приёмка следующего этапа

Один целевой campaign, независимая приёмка и один consolidated corrective pass; повторять только invalidated checks. Два decision gates: утверждение scope и приёмка офлайн результата. Hygiene — отдельная обязательная проверка; live/публикация не следуют из этих gates.

| ID | Поведение и достаточная проверка |
|---|---|
| AC01 | Реальный dispatch через изолированный PluginManager: UNWIRED HOLD, затем `wire()` с reference port; submit сохраняет canonical identity; unwire снова закрывает port. Ноль network/native/home эффектов. |
| AC02 | Same-key/same-payload повтор после закрытия и нового открытия БД возвращает тот же canonical job/result; другой operation/payload/project namespace не смешивается. Конфликт даёт ноль effects. |
| AC03 | Два независимых owner connection одновременно claim одного effect через event/barrier: один dispatch ticket. Чужой token и просроченная epoch не пишут state и не вызывают эффект; takeover не сбрасывает ticket. |
| AC04 | Нет fence, неверная job/revision/attempt/native-start identity или stale epoch: HOLD до cancel, effect counter = 0. Проверить TOCTOU на receiver: epoch меняется между admission и effect, старый token отказан. |
| AC05 | Crash boundaries: до durable intent; после commit до effect; после effect до receipt; после result commit до доставки. После reopen ожидается либо безопасный первый dispatch до intent, либо UNKNOWN/сохранённый результат. Автоматического второго native cancel нет. |
| AC06 | Потерян первый ответ, reopen owner, явная повторная доставка тем же ключом и новый cancel request ID того же attempt: суммарный dispatch counter ≤ 1. Counter принадлежит долгоживущему synthetic sink и не обнуляется вместе с port. |
| AC07 | UNKNOWN → read-only reconcile: terminal proof отсутствует/partial/foreign/stale/wrong binding — HOLD; полный independent proof — разрешённая проекция claims. Read-only сверка не вызывает cancel/start. Reservation удерживается в обеих ветках. |
| AC08 | Worker Done/exit-zero/self-report не доказательство stop; отсутствие terminal proof сохраняет `worker_stopped=False`. Полный fake-положительный контроль явно маркируется synthetic, не live. |
| AC09 | Relevant existing adapter/plugin/cancel-S2/registry/toolsets tests, new targeted file, Ruff, repository aggregate, plugin validate и full suite. NOT_RUN/infra failure фиксируется отдельно от product FAIL. Linux только в существующей разрешённой среде. |

## Риски, восстановление и stop conditions

- Недоступный Bash/TemporaryDirectory в текущем sandbox может заблокировать E1/E2; не менять ACL/sandbox и не заменять обязательный runner bare pytest. Такой код нельзя назвать IMPLEMENTATION_COMPLETE до требуемых проверок.
- Unknown остаётся indefinite при отсутствии независимого observer. Нельзя «лечить» это повторным cancel или освобождением reservation.
- SQLite fixture не квалифицирует production storage, multi-host writer или power-loss durability. Synthetic fenced sink не доказывает поддержку fence настоящим native backend.
- Новые файлы изолированы от runtime. Recovery/rollback — прекратить использование fixture; сохранять БД и evidence UNKNOWN для анализа, не выполнять автоматическое удаление.
- Неразрешённый receiving API/auth, требование новых credentials/окружения, конфликт writer или потребность в записи Symphony — остановить только зависимый этап и продолжить независимую документацию/проверки.

## Решения владельца и следующая граница

Требуется `ok` этой конкретной офлайн-дельты по действующим workspace Workflow Expectations: после CR summary реализация начинается только после explicit `ok`. Просьба продолжать автономно разрешает независимое исследование и подготовку кандидата; approval существующего двухфайлового адаптера не распространяется на новый durable-state контракт автоматически.

Отдельно остаются непринятыми: архитектура in-tree plugin, production ingress/storage/transport и каталог Symphony для записи, credentials/auth/test contour, native backend и независимый terminal observer, live acceptance, merge/publish. Ни READY Hygiene, ни офлайн PASS не заменяют эти решения.
