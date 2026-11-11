# Корректирующий проход: 0 PASS, 5 FAIL; topology UNKNOWN

## Результат разрешённого запуска

Владелец ответил `да` на один корректирующий native no-model проход пяти cases, максимум восемь физических synthetic-процессов, cap 600 s, retries 0. До запуска совпали все 25 hashes reviewed manifest `1f21064d524fe475f4dedf3d098fdd640999e5ecc25df6195f00de644a02ebde`. Штатное повышение доступа разрешило команду.

Команда: `python -X utf8 -I -B .artifacts/serving-windows-20261109/native-crash-correction/run-corrected-campaign.py`. Canonical runner: `scripts/run_tests.sh -j 1 --file-retries 0 --file-timeout 300 tests/plugins/test_symphony_pm_windows_crash.py -v -k "lost_ack or serving_crash or takeover"`. Existing interpreter, temp HOME/runtime/TEMP, scrubbed env/offline/no lazy installs. Никакой установки, модели, микрофона, внешней речи или оплачиваемого ресурса.

**0 PASS, 5 FAIL, 2 deselected**, exit 1. Полная команда **70.188 s**, runner **67.2 s**. Повторов нет. Grant `CONSUMED_FINISHED_FAIL`, remaining=0. Все 25 hashes совпали после выполнения; source changes `[]`.

| Case | Где остановился |
|---|---|
| takeover | До cancel/race: running worker Job не соответствует предположению active=1/exact root |
| lost_ack | До cancel/lost ACK: тот же UNEXPECTED_SYNTHETIC_PROCESS_TOPOLOGY |
| before_assign | Exact serving PID+creation подтвердились, receipt сохранён; ожидание parent Job с двумя members не прошло |
| before_resume | Exact serving PID+creation подтвердились, receipt сохранён; ожидание parent Job с двумя members не прошло |
| after_effect | Receipt cut barrier не появился за 30 s; причина не доказана. Возможен cascade от того же rig.ready(), пока ThreadPoolExecutor ожидает future; это не доказанный отказ cancel |

Исправление redirector identity сработало в двух сохранившихся serving receipts. Из этого не следует, что остальные topology assumptions верны. Фактические active/PID-list значения при mismatch не сохранялись, поэтому **предел восьми физических процессов не подтверждён**. Нельзя заменять неизвестные дополнительные members предположением о conhost или игнорировать их по имени.

Лог SHA256: `cc115f7b299559c62aaede67a594a83ba94b19d6929064a22ff015027c0c9e22`. Квитанции: `.artifacts/serving-windows-20261109/native-crash-correction/{campaign-plan.json,campaign-attempt.json,campaign.log,campaign-result.json,verification.json}`. Preserved proofs: `proofs/serving-before_assign.json`, `proofs/serving-before_resume.json`. Предыдущие результаты не переписаны.

## Cleanup и readback

Parent containment cleanup не сообщил отдельного сбоя; before_assign/before_resume не получили разрешение CUT, так как topology gate остановил parent раньше. Query wait в finally проверяет известные process handles; полный состав Job не сохранён, поэтому полная descendant quiescence не заявляется.

Дополнительное read-only чтение сопоставило exact PID+creation из двух receipts: serving 8000/root 20540 и serving 22428/root 18716. Все четыре PID отсутствовали на момент readback. Квитанция `post-run-known-identities.json`; это bounded known-identity check, не глобальное сканирование/cleanup. Чужие процессы не менялись. Отсутствие этих четырёх процессов не подменяет proof всех неизвестных descendants.

## E0-разбор вместо следующего слепого повторения

Независимый reviewer проверил текущий код и первичные Microsoft docs только чтением. Порядок/типы `Accounting`, query classes 1 и 3 соответствуют документам; nested PID list включает текущий Job и child Jobs. Документация не даёт оснований исключать дополнительные members или считать расхождение допустимым двойным счётом:

- [Basic accounting](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information).
- [Basic process ID list](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_process_id_list).
- [Nested Jobs](https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs).

Текущего evidence недостаточно различить дополнительные OS/helper процессы, неожиданную hierarchy и readback/layout defect. **Production snapshot/fencing не менялись; topology assertions не ослаблены.** Повтор пяти cases на новой догадке не подготовлен и не запускается.

## Подготовленная узкая диагностика: NOT_RUN

Новый файл `tests/plugins/test_symphony_pm_windows_topology_diagnostic.py` содержит один diagnostic case и один synthetic Python launch. Он не вызывает cancel/TerminateJobObject, не имитирует serving crash, не запускает модель или CodexDriver.

Test-owned Job получает kernel `ACTIVE_PROCESS` limit **4 одновременных members** и kill-on-close без breakaway. Это точная граница Job, **не обещание глобального или суммарного lifetime process count**. После наблюдения Job закрывается как owned kernel fail-safe, затем проверяются сохранённые query handles; это не cancel ACK. Повторный native terminate не выполняется.

До resume и при running marker сохраняются fsynced snapshots: полный accounting, query ReturnLength, Assigned/Count, ограниченный raw PID prefix/complete flag, struct sizes/offsets, physical base executable/host metadata. Для проверенных Job members сохраняются PID+creation, query-only image, PPID, signaled; недоступность маркируется UNKNOWN. Raw snapshot сохраняется **до** assertions/metadata queries. Count/Assigned вне 0..4 или incomplete list отказывают до enumeration. Непроверенный/чужой member handle закрывается без image/PPID/wait/control. Созданный exact root имеет отдельный заранее сохранённый query anchor. Close errors и bounded terminal readback попадают в fsynced proof до возврата ошибки.

Prepared scope: **один diagnostic case, один synthetic Python launch, kernel limit четыре одновременных members собственного Job, cap 120 s, file timeout 90 s, retries 0**, без cancel/crash/model/mic/install/расходов. Команда `python -X utf8 -I -B .artifacts/serving-windows-20261109/topology-diagnostic-candidate/run-topology-diagnostic.py`. Plan `PREPARED_NOT_APPROVED_NOT_RUN`, `owner_approved: false`. Этот запуск требует нового отдельного допуска.

Manifest `.artifacts/serving-windows-20261109/topology-diagnostic-candidate/syntax-and-source-manifest.json`, SHA256 `8942a561e4813409f63d700ee4dd419e2580aa671e76c367ddbb9235ba443991`; 26 source/report files. Выполнены только AST/source readback. Диагностический worker, OS probe или test campaign не запускались.

## Текущий статус

HEAD `16ea4b7abbb2541c61c7059f3bde6b3c784d261a`, ветка `codex/hermes-symphony-cancel-correction-20261106`; чужие изменения сохранены, индекс не использовался. Новый CODE не committed/pushed/installed.

Native cancel/release **HOLD**. Полная crash matrix не пройдена. Production create→Job gap остаётся; qualified production approval/driver/environment bindings и реальный Codex stop не подтверждены. Voice readiness, live RU и external/billing stop остаются отдельными неизвестными. Ни один неуспешный native grant не переиспользуется и не сбрасывается новым CR.
