# Crash matrix: 3 PASS, 4 FAIL; корректирующий harness NOT_RUN

## Выполненный допуск

Владелец ответил `да` на семь cases, максимум девять synthetic-процессов, один проход cap 600 s, без retries/model/install. До запуска совпали все 25 hashes reviewed manifest `ebcbaf0421a643c67b6a78b4090cac6428563c2910f85f8ca22375fd40625d8a`. Штатное повышение доступа разрешило исполнение.

Команда: `python -X utf8 -I -B .artifacts/serving-windows-20261109/native-crash-candidate/run-crash-campaign.py`. Canonical runner: `scripts/run_tests.sh -j 1 --file-retries 0 --file-timeout 300 tests/plugins/test_symphony_pm_windows_crash.py -v`. Выделенные HOME/runtime/TEMP, existing test interpreter, scrubbed env/offline/no lazy installs.

Результат: **3 PASS, 4 FAIL**, exit 1. Полная команда **38.985 s**, runner **32.7 s**. Pytest сообщил 7 collected items; исходная оценка runner `~4` не является фактическим числом cases. Повторов нет. Plan помечен `CONSUMED_FINISHED_FAIL`, remaining=0. Native/model scope не расширялся после отказа.

| Case | Результат и предел вывода |
|---|---|
| predecessor creation mismatch | PASS: несовпадение creation identity означает HOLD |
| takeover/native entry/late callback | PASS на исходной topology: rotation конкурирует за lock, stale callback не выполняет effect |
| observer loss | PASS на исходной topology: intent остаётся spent, terminate не повторяется |
| lost ACK | FAIL: прямой test journal.effect() получил STORE_OUTCOME_UNKNOWN при commit, SQLite database is locked |
| before_assign | FAIL до cut release: serving PID 8408 не равен созданному root PID 14124 |
| before_resume | FAIL до cut release: serving PID 25560 не равен root PID 15000 |
| after_effect | FAIL до cut release: serving PID 9980 не равен root PID 27560 |

Три последних результата НЕ доказывают ни выполнение planned crash cut, ни восстановление. after_effect helper мог уже выполнить внутренний effect перед формированием receipt; parent crash/recovery sequence при этом не завершена.

## Причины и нарушения предположений

В harness ошибочно использован `sys.executable` из Windows venv как physical serving root. Его redirector создал другой Python process, что выявила строгая PID-проверка. Она не ослаблялась, descendant PID не признаётся той же identity. `pyvenv.cfg` указывает существующий base interpreter; прежний project-local controller helper уже использует `sys._base_executable` и dependency bootstrap без redirector.

**Заявленный предел девяти физических synthetic-процессов не обеспечен и не подтверждён.** Три наблюдаемые пары helper root/serving уже означают дополнительные процессы сверх предполагаемой topology; worker redirectors также не были отдельно посчитаны. Нельзя переименовать их в «не считающиеся» процессы или заявить соблюдение cap. В корректирующем кандидате используются physical base Python и явные проверки Job membership/count.

lost-ACK fail вызван чтением Journal напрямую из теста без `owner.lock`, пока submit future ещё записывал result. Journal корректно вернул STORE_OUTCOME_UNKNOWN; production timeout/retry/fencing не менялись ради прохождения теста. Коррекция ожидает завершения result writer и сериализует test reads тем же owner lock, включая аналогичное чтение takeover case.

## Cleanup и evidence limits

Parent containment Job finally paths завершились без отдельного сообщения cleanup failure; planned parent CUT не выдавался после identity mismatch. Для всех трёх helper cases проверка завершения известных созданных root handles находится в finally. Но actual serving/worker descendants не имели полного сохранённого независимого query readback. Поэтому полный descendant quiescence не заявляется только по факту Job close.

Pytest scratch cleanup удалил временные cut/readback files после failed file. В журнале сохранены PID mismatch, но нет полного durable topology proof. Для следующего кандидата receipts вынесены в task-owned proofs через уже существующий test infrastructure knob `HERMES_E2E_ARTIFACTS`.

Последующее чтение известных PID не использовалось для убийства процессов. PID 9980 был уже переиспользован: текущий процесс создан `2026-11-09T19:01:09.7741177Z`, позже окончания кампании `2026-11-09T19:01:03.948Z`; он не затрагивался. Отсутствие других прежних PID само по себе не является полным доказательством quiescence всех descendants. Чужие процессы не останавливались.

Все 25 исходных hashes совпали непосредственно после кампании, ДО коррекции. Log SHA256: `1f79de00537dad8375ad403e072254c2b749588ddb1215c24c0eafdeba1a361b`. Квитанции сохранены в `.artifacts/serving-windows-20261109/native-crash-candidate/`. Исторический manifest и raw result не переписаны под исправленный source.

## Коррекция только test harness: подготовлено, не запущено

Изменены только `tests/plugins/symphony_native_crash_support.py` и `test_symphony_pm_windows_crash.py`:

- Physical base interpreter разрешается через hermes_platform; helper получает прежние dependency paths через существующий bootstrap pattern. Worker использует stdlib-only base Python. Никакого install/probe/provider fallback.
- Явные test-owned HOME/USERPROFILE/HERMES_HOME/appdata/temp/runtime для child до импорта Hermes, offline/no-lazy flags.
- Проверяется фактическая topology: один root в worker Job, helper+root в parent Job до эффекта, helper-only после завершения effect. Exact PID+creation остаются обязательными. Проверки topology используют только bounded queries, не дополнительные effects.
- lost ACK ждёт result future до чтения journal; все прямые journal reads в затронутых тестах берут owner.lock.
- Cut receipt сохраняется и fsync до crash; final readback сохраняется в proofs, но не объявляется fsynced. Parent containment остаётся test cleanup и не исправляет production create→assign gap.

Новый candidate manifest: `.artifacts/serving-windows-20261109/native-crash-correction/syntax-and-source-manifest.json`, SHA256 `1f21064d524fe475f4dedf3d098fdd640999e5ecc25df6195f00de644a02ebde`. AST syntax прошёл. Ни импорты исправленного harness, ни новый native/test проход не выполнялись. Текущий HEAD остаётся `16ea4b7abbb2541c61c7059f3bde6b3c784d261a`; исправления локальны, commit/push/install отсутствуют.

Подготовленный узкий следующий допуск: **пять затронутых cases**, selection `lost_ack or serving_crash or takeover`, максимум **восемь физических synthetic-процессов** (два roots плюс три helper/root pairs), один проход cap **600 s**, file timeout **300 s**, retries **0**, без моделей/микрофона/сети/расходов/установки. Четыре упавших cases плюс takeover, где исправлено аналогичное test read. Не повторяются mismatch и observer-loss cases. Plan `PREPARED_NOT_APPROVED_NOT_RUN`, `owner_approved: false`; команда `python -X utf8 -I -B .artifacts/serving-windows-20261109/native-crash-correction/run-corrected-campaign.py`.

## Статус

Native cancel/release **HOLD**. Production create→Job assignment gap сохраняется; полный crash/recovery PASS отсутствует. Fixture 26 PASS и native component 4 PASS остаются отдельным прежним evidence, не заменяют этот неуспешный проход. Voice readiness, настоящий Codex stop, remote inference и billing не подтверждены. Для нового operational запуска требуется отдельный допуск; израсходованный grant не сбрасывается.
