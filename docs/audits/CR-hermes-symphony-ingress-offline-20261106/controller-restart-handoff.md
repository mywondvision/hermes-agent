# Handoff: lifecycle owner/observer и serving transport

**Controller restart PASS в границах синтетического probe; общий cancel HOLD.**

Рабочий каталог: `D:\AI\worktrees\hermes-pm-ingress-20261031`. Ветка: `codex/hermes-symphony-cancel-correction-20261106`. Проверенная реализация — `b766aecb8524a5c0b78197cda7a144473af4fd07`; предшествующий code-review/handoff checkpoint — `fda3bda4bffece8bc3cca7e11be8dd8e72eef91d`. Начни со сверки фактических HEAD, статуса и worktrees. Сохраняй посторонние изменения; шесть прежних отличий и untracked-материалы не входят в новую дельту.

## Канонические материалы нового выполнения

- `.artifacts/controller-restart-20261106/REPORT.md` — команды, версии, идентичности, границы и результаты.
- `.artifacts/controller-restart-20261106/verification.json` — 22 PASS, 0 FAIL.
- `.artifacts/controller-restart-20261106/durable-readback.json` — выгрузка SQLite mode=ro.
- `.artifacts/controller-restart-20261106/post-exit-os-readback.json` — независимые query-only OS-наблюдения после выхода probe.
- `.artifacts/controller-restart-20261106/package-hashes.json` — SHA256 всех 30 файлов исходного evidence snapshot, кроме самого manifest. SHA256 manifest: `b0f17f09111d5789e4d4b31453f338ea359fb9ff6daae9808296a9053cadea90`.
- `docs/audits/CR-hermes-symphony-ingress-offline-20261106/cancel-correction-handoff.md` — прежняя corrective-дельта и независимое ревью.

Probe выполнен один раз: `execution-9605751bba1f4ac28ceacab5232577a4`. C1 PID 10836 завершился после настоящего native effect до durable ack; новый C2 PID 23496 восстановил journal, epoch 1→2, с `receiver=None`. Handles C1 второму контроллеру не передавались. Независимый parent observer оставался жив с query-only handles.

Attempt: `restart-attempt-cec1dd395e674103b6a5042f2a7d4994`. Effect: `c317125acb759bc9014bd5a173c4446c67b641b9e9ef2d6b0d742752a63919fe`. Ровно одна native entry; C2 обработал spent UNKNOWN через OS readback без повторного cancel и нового worker. Остановка реального синтетического Python root/child/conhost подтверждена. Сохранены `LOCAL_STOPPED_HOLD` и `POSSIBLY_CONSUMED_HELD`.

Не проверены: restart самого observer, power loss, остановка настоящего Codex worker при таком восстановлении, remote inference, billing и внешние последствия. Локальный stop не снимает общий HOLD. Успешный прежний Codex smoke повторять не нужно.

## Следующий шаг

1. Прочитай действующие AGENTS.md и актуальные owner, LivePort, cancellation coordinator, plugin `wire()`. Найди реальный Codex driver `runtimes/codex/orchestration/driver.py` в актуальном ките и установи возможности переиспользования. Fixture owner и sandbox probe не являются готовым production transport.
2. Подготовь отдельную CR-дельту lifecycle owner/observer в serving process. Определи владельца OS lifetime/handles, durable identity, admission, fencing, смену epoch и восстановление после потери controller/observer. При spent UNKNOWN запрещены новый dispatch и release резервации без достаточной сверки.
3. Предложи минимальное подключение Hermes plugin → один owner/port → существующий исполнитель. Новая очередь и HTTP-сервер для этого не нужны. Frozen S2 не является scheduler; не открывай его подменой флагов.
4. Зафиксируй acceptance criteria и план отдельных no-model проверок: stale writer, чужой PID/start, потеря observer, восстановление UNKNOWN, отсутствие повторного cancel и нового worker.
5. Покажи CR: scope, исключения, проверка, риски и rollback. Пройди применимый Hygiene Gate. До отдельного `ok` реализацию новой дельты не начинай.

Допуск следующей стадии: чтение, анализ и подготовка CR. Новые модели, Codex workers, live-прогоны, production, ключи и постоянные доступы не разрешены. Для настоящего transport pilot нужны отдельное согласование модели, среды и точного количества attempts. Разрешение текущей сессии на Git publication не является разрешением на будущие push/merge.

Верни минимальный дизайн, конкретные недостающие компоненты и раздельные статусы доказанного, подготовленного и UNKNOWN.

## Публикационная дельта

Пользователь отдельно поручил `commit'n'push` после выполнения probe и выдачи handoff. К публикуемой дельте относятся исходный evidence snapshot (31 файл вместе с manifest), этот handoff и узкое правило `.artifacts/.gitattributes` для сохранения байтов нового пакета. Исходники из ревью и прежние evidence не меняются. Слова LOCAL_ONLY/«не опубликован» в исходных execution receipts описывают состояние на момент их записи; они сохранены неизменными. Фактический результат публикации проверяется по GitHub ref/commit после push. Runtime PASS этим Git-действием не расширяется.
