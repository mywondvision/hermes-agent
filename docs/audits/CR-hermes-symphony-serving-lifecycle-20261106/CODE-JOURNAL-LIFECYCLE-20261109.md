# Serving transport: подготовленный journal/lifecycle CODE

2026-11-09. Статус **FIXTURE_CONTRACT_PASS / NATIVE_HOLD**. Это продолжение уже разрешённого CODE scope по CR `docs/change-requests/CR-hermes-symphony-serving-lifecycle-20261106.md`, а не новая установка или новый runtime. После подготовки владелец отдельно ответил `да` на один focused no-model проход: четыре файла, до 600 секунд, без повторов, моделей, workers, микрофона и установки. Проход выполнен один раз; grant израсходован. Канонический CR и Hygiene report не изменены этим шагом.

## Проверенная база и владение

Текущий HEAD `8169f1b66a3a7c6844e3347c89f5cfa82edd4483`, ветка `codex/hermes-symphony-cancel-correction-20261106`; index пуст. Грязное дерево и прежний evidence сохранены. В доступной app-server inventory активна текущая сессия интегратора `01a1b1c5-d042-7a41-9a2b-cbf70b8b3495`; отдельная serving-сессия `01a1b240-1c23-77b2-8134-b00036161e90` имеет статус notLoaded. Это ограниченная inventory, не доказательство отсутствия любого внешнего OS writer. Нового агента/writer не создано.

Установленный изолированный S3 source остаётся `c419cf7c864ac0ee511dbd9f7091a8bba0b567ee`; новая интеграция туда не перенесена. Read-only kit остаётся `048d446ba484d5d3ca8b052210dbeed3e18d7fb8`; его driver и другой worktree не менялись. Bimo/его ресурсы не запускались и не изменялись; прошлый read-only intake не перепроверялся арендой или replay.

## Контракты и файлы

Владелец перечисленного CODE — единственный Hermes интегратор. Пакет `plugins/symphony_pm/transport/` не импортирует fixtures, smoke или runtime из `.artifacts`; не создаёт scheduler, очередь, HTTP, отдельный Hermes либо второй Symphony.

| Файл | Подготовленное поведение | Граница |
|---|---|---|
| `types.py` | Immutable owner/process/writer/request/plan/permit/attempt/native binding; cancel key неизменен при смене request reason/controller epoch | Значения process identity сами по себе не являются OS proof |
| `schema.py` | Собственная versioned схема; явная миграция legacy с сохранением rows/spent effects и recovery HOLD; отказ чужой/неполной/неподдерживаемой БД | Миграция не восстанавливает grant/handles/permission to resume |
| `journal.py` | Private profile+namespace SQLite; transaction-local fencing, permit correlation, budget spend + LAUNCH_INTENT до эффекта, native binding/RESUME_INTENT, cancel intent, причины и readback отдельно | Неопределённая незакрытая попытка блокирует следующую; completion/release/reconciliation не реализованы |
| `approval.py` | Мост к существующему host-owned `RegisteredApprovalTransport`; correlated once/deny exact native plan; authority getter только читает ранее выданный exact permit | Реальные transport/recipient, модель/effort/environment и native permit не выбраны; consent на CODE не является permit |
| `lifecycle.py` | Один owner и один cancel coordinator для PM_CANCEL/TIMEOUT/EMERGENCY_CLEANUP; attempt-bound runner; intent-before-effect; неопределённый send slot не возвращается; bounded timeout drain; immediate owned fail-safe close при pre-resume error | Конкретные qualified OS guard/query observer/native receiver и атомарное receiver fencing ещё отсутствуют |
| `port.py` | Проекция на существующий PM Protocol, приватный result digest, retention/HOLD; локальная остановка не превращается в cancel ACK | Нет утверждения worker/external/billing settlement |
| `bootstrap.py` | Явная сборка owning host с cleanup acquired guard до дальнейших factory; отсутствующий provider не заменяется default | Не сконфигурирован и не установлен в serving host |
| `../service.py` | Optional host-only `ServingBootstrap`; factory вызывается только service lifecycle после discovery | Без явного binding прежний UNAVAILABLE сохраняется |

`request_permit()` — явная операция trusted host для точного native запуска. Она не вызывается при каждом tool request и не задаёт конечный голосовой UX. Обычная диктовка/ответ и согласования по характеру действия остаются политикой Hermes. `authority_source()` никогда не показывает UI и не выдаёт согласие из текста модели. Internal STOP не зависит от срока human grant; query-only proof локальной остановки сохраняет общий HOLD.

Native factories должны предоставить проверку настоящего OS ownership/predecessor death, exact PID+creation/boot/Job, query-only non-inheritable handles, suspended create, fencing непосредственно в receiver native entry и kill-on-close. Текущий injected Protocol не является их реализацией. Existing CodexDriver launcher должен сохранить его модель/effort/budget/bypass/schema/result проверки без новых probes или альтернативного spawn. Это блокеры установки/native admission. Автоматического выбора модели, доступа, отправки сообщения человеку или аудио текущая подготовка не делала.

## Выполненные проверки

Команда source-only проверки, без project imports или исполнения тестов:

```powershell
python -X utf8 -I -B .artifacts/serving-lifecycle-implementation-20261109/transport/static_check.py
```

AST parse/compile и размеры проверены для 15 Python-файлов; scoped whitespace/diff check PASS. Это проверка синтаксиса, не pytest, CI lint либо поведенческий PASS. Source manifest: `.artifacts/serving-lifecycle-implementation-20261109/transport/syntax-and-source-manifest.json`.

Общий `git diff --check` exit 2: ранее существующий `.artifacts/live-smoke-20261106/events.jsonl` содержит trailing whitespace в строках 12–21; evidence не редактировался. Полный вывод сохранён в `transport/worktree-diff-check.txt`.

Предыдущая отдельная campaign действительно дала **114 PASS / 0 FAIL** на 8 файлах, runner 132.1 s, команда 135.703 s. Она не повторена. С тех пор изменились `plugins/symphony_pm/service.py` и `tests/plugins/test_symphony_pm_serving.py`; новые transport modules не были в прежнем manifest. Поэтому 114 PASS — историческая квалификация прежнего generic binding, не новый journal/lifecycle PASS.

Выполнены **24 PASS / 0 FAIL**, четыре targeted файла, один serial проход, без file retries. Время команды 89.859 s, runner 82.8 s. После завершения все 15 source hashes совпали с pre-campaign manifest; во время кампании код не менялся. Результат не складывается с прежними 114 PASS как новая полная квалификация: четыре прежних serving-теста входят в оба прохода.

| Файл | PASS | Время файла |
|---|---:|---:|
| `tests/plugins/test_symphony_pm_journal.py` | 6 | 32.1 s |
| `tests/plugins/test_symphony_pm_lifecycle.py` | 7 | 15.8 s |
| `tests/plugins/test_symphony_pm_permit_source.py` | 5 | 13.3 s |
| `tests/plugins/test_symphony_pm_serving.py` | 6 | 20.9 s |

Providers исключительно fixture/in-process: fake guard/observer/receiver, temp SQLite, correlated fake approval callback; canonical runner создавал pytest subprocesses. Настоящих native workers/моделей/микрофона/внешней речи/аренды не запускали. Проверки включают spent budget/replay, uncertain unbound intent, controller fencing, legacy migration, incomplete schema refusal, timeout/cleanup dedupe, lost cancel ACK, recovery without receiver, local stop с held reservation, storage cut после create до resume, permit mismatch/denial/expiry/foreign profile/stale controller, deferred bootstrap и rollback factory failure. Это компонентная проверка journal/state machine/authority/discovery, не OS crash matrix или приёмка полного transport.

Квитанции: `.artifacts/serving-lifecycle-implementation-20261109/transport/{campaign-attempt.json,campaign.log,campaign-result.json,campaign-proof.json}`. Proof содержит exact argv, per-file counts, длительности, совпадение hashes и `campaign_remaining: 0`. Эти времена не являются voice или inference latency.

## Выполненная команда и дальнейший шаг

CR § «Acceptance и отдельная no-model кампания (NOT_RUN)» требует «После отдельного разрешения» для no-model E1/E2. Отдельное разрешение на этот focused проход получено и израсходовано: ровно 4 файла, `-j 1`, `--file-retries 0`, общий cap 600 s; без native worker/model/audio/paid/install/production/commit/push. Это не повтор 8-файлового прохода и не новый Hygiene review. Повтор успешного прохода не требуется.

Фактически выполненная команда (не повторять по этому grant):

```powershell
python -X utf8 -I -B .artifacts/serving-lifecycle-implementation-20261109/transport/run-transport-campaign.py
```

Точные runner argv:

```text
bash scripts/run_tests.sh -j 1 --file-retries 0 --file-timeout 60
  tests/plugins/test_symphony_pm_journal.py
  tests/plugins/test_symphony_pm_lifecycle.py
  tests/plugins/test_symphony_pm_permit_source.py
  tests/plugins/test_symphony_pm_serving.py
```

Wrapper использовал существующий `D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe`, выделенные HOME/runtime/TEMP, scrubbed env, offline и disabled lazy installs; перед запуском сверил source hashes и записал одноразовый attempt marker. `campaign-plan.json` теперь `CONSUMED_FINISHED`, remaining 0; marker/result сохранены. Timeout не наступил. При будущих иных bounded проверках timeout сам по себе не доказывает quiescence descendants. Parent correction/native attempt/effect бюджеты не обнулены.

Точный следующий участок исходного CODE scope: подготовить конкретные Windows OwnershipGuard, query-only observer, attempt-bound native receiver с atomic fencing/Job coverage и launcher на существующем CodexDriver; сохранить отсутствие запуска по умолчанию. До их квалификации transport не устанавливать и native admission не открывать. Следующая native crash matrix или pilot потребует собственного конечного допуска; model pilot дополнительно требует точной модели/effort/среды/attempt permit. Нового допуска на эти действия текущая успешная fixture campaign не создаёт.

## Общая голосовая цель

| Участок | Статус |
|---|---|
| Изолированный S3 runtime | Source pin подтверждён; текущий transport CODE не установлен |
| Global voice hotkey из другого окна | NOT_IMPLEMENTED; terminal Ctrl+B/Quick Entry text не закрывают критерий |
| Русская живая речь → ответ | NOT_RUN; нет данного здесь mic grant и согласованного answer model/provider |
| Новая Hermes → Symphony задача | NOT_RUN; qualified native providers/launcher и exact permit не закреплены |
| ASR cancel / запись | Не используется как worker proof |
| Общий worker cancel/release | HOLD; local stop отдельно, external effects/billing UNKNOWN |

OpenWhispr 1.10.1 сохранён. Bimo остаётся заменяемым ASR/retrieval/GPU backend и владельцем своих файлов; не получает локальное состояние или единственный STOP. Синтетические RU/EN не принимаются за качество живой речи. Voice latency и полная end-to-end latency не измерялись этим шагом.

Откат этой неустановленной дельты — не включать новый bootstrap/admissions. При будущем включении сохранять journal/identities/effects/reservations и закрывать только собственные handles через owner cleanup; spent intent не откатывать snapshot/downgrade. Полную голосовую цепочку пока не принимать.
