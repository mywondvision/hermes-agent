# CR-hermes-symphony-ingress-offline-20261106: корректирующая дельта cancel

context_hygiene: PASS rev=97d0f1cbd4a5 report=docs/audits/CR-hermes-symphony-ingress-offline-20261106/cancel-correction-hygiene.json

Линия: существующий CR-hermes-symphony-ingress-offline-20261106, один corrective pass после независимого live-smoke review. Допуск на перечисленную коррекцию — текущий явный запрос владельца; это не новая авторизация live worker. Исходный CR и принятые доказательства не переписываются. Профиль Standard/R2, proof cap E1 для логики и офлайн crash/recovery; новый OS/controller restart test только подготовить, NOT RUN.

## Цель и область

Направить PM cancel, timeout и аварийную очистку через один согласованный механизм с устойчивой идентичностью эффекта. Сохранить различия причин и не ослаблять защитные тайм-ауты.

Не отправлять второй native cancel при UNKNOWN: сначала сверка состояния. HOLD и удержание резервации сохраняются, пока остановка и последствия не подтверждены.

Добавить офлайн-тесты гонки timeout с PM cancel, повторной доставки с другим request ID, потери ответа, падения до и после эффекта и восстановления.

Усилить verifier отрицательными fixtures: пустые наборы доказательств отвергать; проверять связи request/job/attempt/PID и хэши опубликованного пакета.

Уточнить существующее свидетельство: выполнялся reopen базы с тем же живым LivePort и OS handles, а не перезапуск контроллера.

После исправления подготовить минимальный отдельный sandbox-тест настоящего controller restart; не называть его пройденным заранее.

Передать итоговую дельту на независимое ревью и вернуть точные SHA, тесты, оставшиеся UNKNOWN и следующий шаг к работающему транспорту.

## Границы допуска

Успешный запуск настоящего Codex и локальная остановка дерева процессов приняты по опубликованным доказательствам ebcc60d2644df20bf2d8ca70735b29d8da3ee78e; общий cancel остаётся HOLD. Полностью повторять успешный smoke не нужно.

Новый полный live-прогон и новые native Codex-сессии не разрешены этой дельтой. Перед новыми Codex-сессиями требуется согласование модели и среды. Отдельно согласована одна read-only review-сессия GPT-6.1 Sol High в текущем Windows, до двух проходов (Hygiene/итоговый review); она не разрешает worker attempts.

Никакого production, новых ключей, merge и изменения чужих worktree. Сохранять чужие изменения. Приватные исходники kit/S2 не публиковать.

## Dependency impact и реализационный контур

Только task-local harness `.artifacts/live-smoke-20261106/`: `cancel_control.py` (новый), `smoke.py`, `winjob.py`, `verify_evidence.py`, verifier helpers и restart-test preparation. Офлайн-тесты — новые `tests/plugins/test_symphony_smoke_cancel.py`, `tests/plugins/test_symphony_smoke_evidence.py`. Пакеты/документация/доказательства — текущий CR audit и `.artifacts/cancel-correction-20261106/`. Public adapter, plugin API, frozen S2 и существующий offline reference owner сохраняются. Один owner SQLite journal, без второй runnable queue, HTTP-сервера или новой инфраструктуры.

Проверенная база публикации — ebcc60d2644df20bf2d8ca70735b29d8da3ee78e; текущий рабочий HEAD остаётся c419cf7c864ac0ee511dbd9f7091a8bba0b567ee с task-owned untracked материалами. Дельта сравнивается с опубликованными blob, не с предположением о совпадении HEAD. Принятые live artifacts immutable; исправления их интерпретации публикуются отдельно.

## Приёмка

1. Все явные cancel причины используют один durable effect key exact attempt/native binding; новый request ID/причина/epoch не создаёт второй ticket. SQL intent фиксируется до native dispatch; receiver дополнительно защищён от повторного вызова. Политика kernel kill-on-close остаётся fail-safe; её независимые последствия не выдаются за подтверждённый receipt или за controller restart proof.
2. Гонка, потеря ответа, crash boundaries, reopen и recovery подтверждены офлайн-тестами на настоящем SQLite; UNKNOWN не приводит к повторной native отправке. Deadline и emergency-cleanup причины сохраняются в journal. Observer без эффекта, отсутствие/пустота/чужая identity/неполный proof дают HOLD, reservation удержана.
3. Verifier отвергает пустые collections, разорванные request/job/attempt/native binding, неверные временные связи и повреждённые hashes. Старые успешные artifacts сохраняют ограниченный смысл; полный synthetic/restart/live PASS не выводится из проверок согласованности файлов.
4. Подготовка controller restart отделяет процесс C1 от C2 и задаёт exact-target readback, spent-ticket recovery и запрет новой worker attempt. Запуск не выполнен и не заявляется; необходимые модель/среда/допуск указаны до возможного native worker теста.
5. Один targeted canonical campaign без retries и целевой Ruff; повторяются только invalidated проверки. Затем независимый review дельты, SHA и конкретный handoff. Публикация новой дельты — отдельный операционный шаг; допуск на push не выводится из требования независимого review. Merge не разрешён.

## Риски и восстановление

SQL commit и OS effect не образуют общей транзакции: crash после intent может оставить UNKNOWN без фактического cancel. Recovery не сбрасывает spent ticket. Kernel close/process death может завершить дерево без подтверждённого приложениям ответа; это сверяется отдельно. Новый код не доказывает billing settlement или отсутствие внешних эффектов. Изменившиеся кодовые пути теряют прежний overall PASS; исторические read-only JSON и локальный OS stop не повторяются без необходимости.

Исправленный timeout/cancel и настоящий controller restart пока не проверены; external effects/billing settlement остаются UNKNOWN и общий cancel HOLD.

Без запуска модели откатить только task-owned candidate-код по опубликованным blob при явном решении; не менять старые effect/state/evidence, не сбрасывать ticket и не трогать чужие worktree.
