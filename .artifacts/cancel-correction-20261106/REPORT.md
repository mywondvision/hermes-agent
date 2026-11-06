# Cancel correction: кандидат на независимый code review

Линия: CR-hermes-symphony-ingress-offline-20261106, corrective pass после live smoke ebcc60d2644df20bf2d8ca70735b29d8da3ee78e. Gate Hygiene PASS/READY по новой ограниченной дельте; разрешена одна read-only review-сессия GPT-6.1 Sol High, два прохода (Hygiene и итоговый code review). Новый native Codex worker и полный повтор live smoke не запускались.

Реализация: `cancel_control.py` использует ту же SQLite owner DB и существующую effects table. До native dispatch сохраняется DISPATCH_INTENT. Разные PM request IDs, TIMEOUT, EMERGENCY_CLEANUP сохраняются отдельно в cancel_causes, но используют один effect key exact attempt. Неопределённый исход после intent никогда не создаёт второй dispatch. Reopen/new epoch/new receiver проверены офлайн; observer-only recovery доступен без прежнего receiver. Все readbacks сохраняются отдельно; локальный stopped не превращается в settlement четырёх claims.

`NativeCancelGate` receiver тратит send slot до OS call и не сбрасывает его при исключении. Raw `WindowsJob.cancel_once()` без durable permit отвергается. PM cancel, runtime timeout и явный close/cleanup зарегистрированного worker вызывают один coordinator. Timeout сохраняет исходные wait limits 180/15, включая ошибку coordinator. Kernel kill-on-close остаётся fail-safe ОС; автоматическое закрытие handle при аварии процесса не выдаётся за acknowledged application cancel. При UNKNOWN explicit cleanup записывает причину, сначала сверяет состояние, не повторяет TerminateJobObject; закрытие Job может активировать kernel fail-safe, а результат остаётся HOLD.

Отдельная граница для review: существующий rollback ошибки создания ещё suspended, не зарегистрированного native worker в `start_suspended` сохраняет `process.kill()` и bounded wait=10. Он выполняется до durable native binding/PM admission и до runtime watchdog, поэтому не может гоняться с ними; это creation rollback, не второй runtime cancel ticket. Он не объявляется квалификацией атомарности CreateProcess+storage или гарантией restart. Просьба reviewer отдельно проверить допустимость этой границы и failure cleanup в prepared restart probe.

Verifier: новая логика читает immutable Git objects по ebcc60d2 или явный published export, проверяет pinned SHA256 PUBLICATION_MANIFEST и artifact-hashes, непустые proof sets, root PID/creation FILETIME, job/revision/attempt, request digest, native thread, ledger links, причинный порядок и reservation/HOLD. Негативные fixtures reseal свои синтетические hash anchors, чтобы действительно проверять семантику cross-file bindings; CLI не принимает переопределение trusted pins. Verifier — проверка согласованности принятых файлов, не новое live-доказательство.

Проверки (canonical `scripts/run_tests.sh`, один worker, file retries=0; существующий Python 3.14.9 / pytest 9.1.1):

- Baseline red: опубликованный winjob.py не отвергает нетикетированный call, тест падает `DID NOT RAISE CancelRejected`; native API заменён счётчиком, OS cancel не выполнялся. `baseline-red.txt`.
- Основной campaign: cancel 19 PASS; verifier 25 PASS + один fixture setup error (`PREPARED` event не имеет payload). `targeted.txt`.
- Исправлен только builder ошибочного fixture; этот case — 1 PASS. `evidence-fix.txt`.
- Изменён timeout drain в finally и добавлены coordinator-error варианты: четыре актуальных timeout case — 4 PASS. `runtime-fix.txt`.
- Покрытие итогового набора: 21 cancel + 26 evidence = **47 уникальных актуальных cases**, с переиспользованием только не затронутых результатов. Фактические команды/прогоны указаны выше; единый выдуманный «47 в одном свежем запуске» не заявляется.
- Целевой Ruff и C901≤20 PASS. Прежние 144/144 относятся к неизменившимся public adapter/plugin/offline-owner, а не к новому runtime коду. Полный repository aggregate не объявляется зелёным.

Настоящий restart: `controller_restart_probe.py prepare` создал plan.json со статусом **NOT_RUN**. Сценарий: один synthetic Python worker + child, C1 durable intent и OS effect, os._exit до ack; отдельный query-only observer сохраняет handles; C2 — новый процесс, новый owner epoch после independent death proof C1, тот же journal, receiver=None, только readback. Подготовка не считается исполнением. Новых сред/ключей/production не создавалось.

Точные команды:

```powershell
& ./.artifacts/cancel-correction-20261106/run-targeted.ps1 -Baseline
& ./.artifacts/cancel-correction-20261106/run-targeted.ps1
& ./.artifacts/cancel-correction-20261106/run-targeted.ps1 -EvidenceFix
& ./.artifacts/cancel-correction-20261106/run-targeted.ps1 -RuntimeFix
& 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe' -B .artifacts/live-smoke-20261106/verify_evidence.py
& 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe' -B .artifacts/live-smoke-20261106/controller_restart_probe.py prepare
```

Осталось UNKNOWN/NOT_RUN: новый native timeout path, настоящий C1→C2 probe, OS failure cleanup сценарии после исправления, последствия kernel close при UNKNOWN, full controller restart/broker lifecycle, production fencing/admission, settlement billing/external effects. Общий cancel HOLD, reservation удержана. Исторические raw live artifacts не изменены. Новая публикация/merge не выводится из code-review запроса; на этом шаге готовится локальная review-дельта.

Следующий достаточный шаг после независимого code review — отдельно разрешить минимальный нулевой-model controller restart probe в текущем Windows. Если он подтвердит cross-process recovery без нового dispatch/launch, связать сохранение/recovery owner с жизненным циклом serving process и квалифицированным observer; новый Codex worker или настоящий транспортный pilot — только по отдельному допуску модели, среды и запуска. HTTP-сервер, новые ключи и повтор успешного smoke для этой коррекции не нужны.
