# Причина topology mismatch подтверждена: Python + conhost

## Разрешённая диагностика

Владелец ответил `ДА` на один synthetic Python launch, cap 120 s, kernel limit четыре одновременных члена собственного Job, без повторов/cancel/crash/model/install. Перед запуском совпали 26 hashes reviewed manifest `8942a561e4813409f63d700ee4dd419e2580aa671e76c367ddbb9235ba443991`.

Команда: `python -X utf8 -I -B .artifacts/serving-windows-20261109/topology-diagnostic-candidate/run-topology-diagnostic.py`. Штатное повышение доступа разрешило запуск. Использованы canonical `scripts/run_tests.sh`, existing interpreter, scrubbed env и выделенные HOME/runtime/TEMP.

**1 PASS, 0 FAIL**, exit 0. Полная команда **20.515 s**, runner **15.9 s**, без retries. Grant `CONSUMED_FINISHED_PASS`, remaining=0. Все 26 source hashes совпали после кампании, до последующей source-калибровки. Cancel/TerminateJobObject и serving crash не вызывались; cleanup — закрытие собственного kill-on-close Job.

## Native readback

| Стадия | Active / Total / Assigned / Count | Exact identities |
|---|---|---|
| Suspended | 1 / 1 / 1 / 1 | Python PID 22124, creation `134387259451575732` |
| Running | 2 / 2 / 2 / 2 | Тот же Python плюс conhost PID 25436, creation `134387259452353528`, PPID 22124 |
| После owned Job close | Сохранённые query handles обоих процессов signaled=true | Обе creation identities совпадают; errors=[] |

Query-only image root: существующий base `.../cpython-3.14.9-windows-x86_64-none/python.exe`; image второго member: `C:/Windows/System32/conhost.exe`. Оба `in_job=true`. Это фактический дополнительный член того же Job после resume, а не гипотеза или имя, по которому разрешён control.

Accounting struct size 48, pointer size 8; offsets 0/8/16/24/32/36/40/44. API ReturnLength 48 для accounting; PID-list ReturnLength 16 suspended и 24 running. Raw accounting и полный bounded PID list согласованы. **Данных за ошибку layout/double-counting здесь нет.** Прежний harness ошибочно ожидал только Python root и не учитывал фактический console host.

Proof fsynced на стадиях и после cleanup: `.artifacts/serving-windows-20261109/topology-diagnostic-candidate/proofs/topology.json`, SHA256 `8dee98e0bb60f20e6dbd2766b6b2493f785a51107f66da0fa95b84c92f9a9048`. Его каталог имеет user-only ACL; обычный sandbox получил Access denied, поэтому квитанция прочитана отдельно с повышенным доступом, только чтением. ACL не ослаблялись. Log SHA256 `ee32833c938688bc5a6d162d15c9f55f93cee242c2ef347e19549c9b0a9efdd5`. Raw result и verification сохранены рядом.

Это diagnostic evidence для одного measured Job. Не доказаны весь serving lifecycle, production cancel ACK, release, Codex/model stop, external inference или billing.

## Калибровка harness после диагностики: NOT_RUN

Исправлены только два test-owned файла: `symphony_native_crash_support.py`, `test_symphony_pm_windows_crash.py`. Production WindowsApi/observer/fencing не менялись.

- Worker Job имеет kernel active limit 2; parent containment Job — 4. Все наблюдаемые members получают retained query-only handles с проверкой Job membership и creation identity; console host **не исключается** из наблюдения или terminal proof по имени.
- Worker ready теперь ожидает два измеренных members. Parent before_assign/before_resume ожидает три (helper + его host + suspended worker); after_effect — два оставшихся helper members. Дополнительно проверяются cumulative Total=2 у worker и Total=3/3/4 у parent.
- Перед эффектами сохраняется fsynced полный roster; mismatch также сохраняется до отказа. Terminal proof собирается для всех измеренных members. Storage error не пропускает остальные cleanup/wait/close попытки.
- В after_effect exact worker root **и console host** проверяются на signal и сохраняются отдельно **до release и при ещё открытом parent containment Job**. Поэтому поздний cleanup parent Job не подменяет доказательство результата исходного worker effect.
- Прежние launch/effect IDs и epochs сохраняются; recovery не получает новый receiver/worker/cancel slot. Тестовый parent containment остаётся только страховкой и не исправляет production create→Job assignment gap.

Независимый reviewer выполнил только статическое чтение, без записи или запусков. Замечания по storage cleanup и независимому pre-cleanup readback внесены; свежим чтением reviewer подтвердил закрытие этих замечаний. Новый runtime PASS не заявляется.

Candidate: `.artifacts/serving-windows-20261109/native-crash-calibrated/`, manifest SHA256 `b3291f80f8a0d6f1de5831d295200e6c7518400b3b797c2d12e79f71ccdd70ba`. AST/source readback выполнены; полный CI/lint не запускался. Plan `PREPARED_NOT_APPROVED_NOT_RUN`, `owner_approved: false`.

## Конкретная граница следующего допуска

Один проход пяти прежних затронутых cases (`lost_ack or serving_crash or takeover`), cap **600 s**, file timeout **300 s**, retries **0**, без моделей/микрофона/сети/расходов/установки. **Восемь явных Python launches плюс шесть ожидаемых и учитываемых console hosts = бюджет 14 физических процессов**: 2 + 2 + 3 + 3 + 4. Kernel concurrent limits 2/4 и cumulative Total checks проверяются отдельно; фактическое соблюдение нового бюджета до запуска не заявляется. Любой другой roster означает HOLD, не игнорирование процесса по имени.

Подготовленная команда: `python -X utf8 -I -B .artifacts/serving-windows-20261109/native-crash-calibrated/run-calibrated-campaign.py`. Нужен новый отдельный допуск с этим увеличенным и явным process budget. Израсходованный diagnostic grant не разрешает этот повтор.

## Текущий итог

HEAD `16ea4b7abbb2541c61c7059f3bde6b3c784d261a`, ветка `codex/hermes-symphony-cancel-correction-20261106`. Чужие изменения сохранены; commit/push/install не выполнялись.

**Native cancel/release HOLD.** Причина предыдущего topology mismatch теперь установлена для worker, cleanup диагностических двух identities подтверждён. Полная crash matrix и новое калиброванное дерево ещё не приняты. Production create→Job gap, реальные driver/issuer/environment bindings, live voice и external/billing stop остаются отдельными незакрытыми критериями.
