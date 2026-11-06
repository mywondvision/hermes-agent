# C1 → C2: выполненный sandbox probe, 2026-11-06

**Механический controller restart PASS; общий cancel HOLD.** Один разрешённый запуск подготовленного probe, exit 0; 22/22 последующих проверок evidence PASS. Повторов и новых Codex/model sessions — 0. Проверяемый worker в этом запуске — настоящий локальный Python-процесс с дочерним Python и conhost, без модели и проектных данных.

## Исходники и сохранность

- Проверенная реализация: `b766aecb8524a5c0b78197cda7a144473af4fd07`.
- Handoff/review и текущий HEAD: `fda3bda4bffece8bc3cca7e11be8dd8e72eef91d`.
- Локальная ветка: `codex/hermes-symphony-cancel-correction-20261106`.
- Начальное состояние: detached HEAD `c419cf7c864ac0ee511dbd9f7091a8bba0b567ee`, без staged/tracked изменений; материалы задачи были untracked.
- Присоединение к указанной ветке сделано через `git read-tree` без `-u`, затем `git symbolic-ref HEAD`; checkout, reset, clean, stash не применялись. Файлы рабочей копии не переписывались.
- Все 14 файлов из независимого code review совпали с проверенными SHA256 до и после запуска. SHA256 probe: `000e90fa0d198f9494f82ff9f55dd606caca4b213cfd4de35f43129e63ef3057`.
- Сохранены raw bytes 159 файлов: overlay ветки, 21 исходник frozen S2 и явно защищённые посторонние/исторические файлы. Исторический `.artifacts/live-smoke-20261106/owner.sqlite` не использовался и не менялся.
- Шесть отличий от целевой ветки существовали до присоединения: пять файлов отличаются только CRLF/LF; исторический `events.jsonl` отличается по содержимому. Все сохранены. Они видны как unstaged modifications; staged diff пуст. `source-survey.json`, `preflight.json`, `branch-attachment.json` фиксируют это явно.
- Второй worktree и его ref `369a012cc9b5caece4b1a6e37fdecb1ca04530b3` не изменялись; ref проверен. Полная побайтная инвентаризация всех untracked runtime caches не заявляется. Пять старых недоступных каталогов перечислены в preflight; тест их не использовал.
- Исходный исторический NOT_RUN plan и прежние опубликованные доказательства не переписаны. Новое выполнение находится только в данном отдельном каталоге.
- Commit, push, merge и изменение production не выполнялись. Новое ревью моделью не запускалось.

## Команды и среда

Точные argv и версии находятся в `invocation.json`; фактически выполнены:

```powershell
git read-tree fda3bda4bffece8bc3cca7e11be8dd8e72eef91d
git symbolic-ref HEAD refs/heads/codex/hermes-symphony-cancel-correction-20261106
& 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe' -B .artifacts/controller-restart-20261106/execute-once.py
& 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe' -B .artifacts/controller-restart-20261106/verify-readback.py
```

`execute-once.py` записывает exclusive marker и единожды выполняет неизменённый reviewed probe:

```powershell
& 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe' -B .artifacts/live-smoke-20261106/controller_restart_probe.py run --execute --directory 'D:/AI/worktrees/hermes-pm-ingress-20261031/.artifacts/controller-restart-20261106/sandbox'
```

Это запись уже выполненной команды, не разрешение на повтор. Автоматических retry нет. Внутри reviewed `controller_command()` запускается существующий base Python с `-B -c` bootstrap/runpy и текущими dependency paths; для C1/C2 используются отдельные процессы и аргументы `c1`/`c2 --execute --directory <execution-dir>`. Новая среда не создавалась.

Версии: Python **3.14.9**, psutil **7.2.2**, Git **2.45.1.windows.1**, Windows **11 / 10.0.26200**. Base Python: `.artifacts/test-appdata/uv/python/cpython-3.14-windows-x86_64-none/python.exe`. Значения environment и секреты в отчёт не выводились.

## Идентичности и фактический сценарий

- Execution: `execution-9605751bba1f4ac28ceacab5232577a4`.
- Submit request: `restart-submit`.
- Cancel request: `internal-pm_cancel-restart-attempt-cec1dd395e674103b6a5042f2a7d4994`.
- Job: `job-da2a7f417a67495bb6b910a34be88b4e`.
- Revision: `dfb21feb37ed7fb114aa427479895070481d91a5e8c31b662711f82c3da93cfb`.
- Attempt: `restart-attempt-cec1dd395e674103b6a5042f2a7d4994`.
- Cancel effect: `c317125acb759bc9014bd5a173c4446c67b641b9e9ef2d6b0d742752a63919fe`.
- C1: PID **10836**, creation FILETIME **134384646756849265**, epoch **1**.
- C2: PID **23496**, epoch **2**, `receiver=None`.
- Worker root: PID **3380**, creation FILETIME **134384646764466132**.
- Conhost: PID **5712**, FILETIME **134384646765929289**.
- Дочерний Python: PID **18844**, FILETIME **134384646767939393**.
- Job Object: `Local\HermesSmoke-restart-attempt-cec1dd395e674103b6a5042f2a7d4994`.

C1 был подтверждён parent по retained process handle и точному PID/creation identity до допуска worker. Затем worker был создан suspended и помещён в Windows Job Object до resume. Parent дождался READY и непустого набора процессов, открыл query-only Job handle и четыре process handles (C1 + три члена Job).

После durable `DISPATCH_INTENT` C1 один раз вошёл в настоящий `TerminateJobObject`. Запись `native-return.json` содержит `acknowledged=true`. После эффекта, до durable finish/ack C1 выполнил `os._exit(23)`. Успешное завершение reviewed parent подтверждает проверку именно этого exit code; отдельный файл exit code C1 самим probe не записывается. После wait C1 parent запустил новый процесс C2 и дождался его exit 0. Внешний монитор независимо увидел разные PID C1/C2 и членов worker Job (`process-observations.jsonl`).

C2 заново открыл process handle C1, подтвердил точную creation identity и signaled, затем сменил writer epoch. Новый экземпляр открыл тот же SQLite journal и нашёл spent `DISPATCH_INTENT`, который `Owner.snapshot()` отображает как `UNKNOWN`. Восстановленный target сохранил job/revision/attempt/PID/start. C2 не получил receiver и не имел пути dispatch. Он заново открыл query-only handles по durable named Job/process identities и сохранил readback.

Read-only здесь относится к OS effect: C2 не вызывал terminate/cancel. Journal закономерно получил нового writer/epoch и запись readback; это не полностью read-only открытие SQLite. Полностью read-only последующая выгрузка выполнена отдельно через SQLite `mode=ro`, без создания Owner/receiver.

**C1 действительно завершён, C2 — другой OS process. Это не reopen базы внутри прежнего LivePort.** In-memory handles C1 второму контроллеру не передавались. Независимый parent observer при этом оставался жив, удерживая собственные non-inheritable query-only handles; это осознанная граница теста. Полный restart observer/машины не проверялся.

## Раздельные результаты

| Статус | Проверенное свойство | Evidence |
|---|---|---|
| PASS | Реальный C1 exit → новый C2, epoch 1→2, прежний journal/target | `c1-identity.json`, `c2-result.json`, `result.json`, `process-observations.jsonl` |
| PASS | Ровно одна записанная native API entry, ack true, один durable effect | `native-calls.jsonl`, `native-return.json`, `durable-readback.json` |
| PASS | UNKNOWN/spent intent → read-only reconcile, без повторного cancel и нового worker | C2 `receiver=null`, `dispatched=false`; один job/effect, неизменённый dispatch epoch 1 |
| PASS | Локальная остановка реального синтетического root/child/conhost | C2 readback: Job active=0, pids=[], три exact PID/start identity_matches/signaled=true; parent проверил четыре retained handles |
| PASS | Последующая независимая read-only OS сверка | `post-exit-os-readback.json`: root, child, conhost и C1 отсутствуют, OpenProcess error 87; cancel/kill не вызывались |
| PASS | HOLD и резервация сохранены | `LOCAL_STOPPED_HOLD`, `POSSIBLY_CONSUMED_HELD`; durable state остаётся `DISPATCH_INTENT`, proof/proof_epoch NULL |
| PASS | 22/22 проверки связей evidence, state, SHA и сохранности | `verification.json`, `verify-readback.py`; exit 0 |
| FAIL | Runtime acceptance failures не обнаружены | 0/22 FAIL, stderr/controllers.out пусты |
| UNKNOWN / NOT_RUN | Остановка настоящего Codex worker при таком restart и remote inference/billing/external settlement | Новых Codex sessions 0; этот probe их не проверяет |
| UNKNOWN / NOT_RUN | Restart самого observer, power loss, atomicity process-create/storage, native-host crash-before-effect/timeout variants | В данном запуске только after-effect/pre-ack cut |
| HOLD | Общий cancel и release резервации | Локальный stop не является подтверждением всех последствий |

Начальный строгий overlay-preflight остановился на расхождениях с веткой до запуска любого процесса теста. Расхождения исследованы, исходники из ревью проверены отдельно; существующие evidence/переводы строк сохранены. Это не скрытый FAIL runtime и не замена reviewed кода. Широкая инвентаризация runtime caches была остановлена без native attempt; итоговая проверка сохранности ограничена явно перечисленными 159 файлами.

Здесь доказаны одна native entry и локальная остановка; причинность остановки исключительно от `TerminateJobObject` отдельно от последующего kernel kill-on-close не изолировалась. Тайм-ауты reviewed probe не менялись. Полный live smoke и прежний offline suite не повторялись. Исторические offline 55 случаев и live Codex success остаются прежними отдельно принятыми результатами, а не новыми PASS этого запуска.

## Следующий конкретный шаг

Подготовить отдельную CR-дельту для lifecycle owner/observer в **serving process**. Сохранить существующий owner journal и один durable effect key. Вынести живущее вне controller владение Job/process lifetime в явный локальный observer/supervisor с минимальными правами, сохранённой identity и определённой политикой смерти/повторного attach. Новый controller получает query/reconcile доступ; при spent UNKNOWN новый sender не создаётся. Проверка predecessor death, смена epoch, fencing и запрет admission новых attempts до восстановления должны стать реальными serving-path checks, а не правилами sandbox probe.

После ревью этой дельты проверить отдельно потерю/восстановление observer на синтетическом worker без модели: stale writer rejected, чужой PID/start rejected, UNKNOWN не посылает cancel повторно, reservation остаётся held. Затем связать opt-in Hermes `wire()` с одним lifecycle owner/port и существующим исполнителем, без новой очереди или HTTP-сервера. Только после этого предлагать ограниченный transport pilot с настоящим Codex worker, с отдельным согласованием модели/среды/точного количества attempts и способом сверки внешних последствий. В этой сессии такой pilot не запускался.

`package-hashes.json` фиксирует SHA256 нового локального evidence-пакета (кроме самого manifest). Пакет не опубликован.
