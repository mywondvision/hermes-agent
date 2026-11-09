# CR: Hermes → Symphony — lifecycle owner и in-process transport

context_hygiene: PASS rev=d408f7b5b619e13ab13fd113b428b7e5633399719d0b7732df7fa66a8d2d691d report=docs/audits/CR-hermes-symphony-serving-lifecycle-20261106/hygiene-d408f7b5b619.json

## Действующая поправка: generic host binding (2026-11-09)

Приоритет: этот раздел уточняет исходную ревизию `c28c4ba9bc9b3b3deb70d61469231da65f5aef3ab7bd468d340f87e55c6e49d1`. Владелец дал `ок` на CODE scope исходного serving CR, затем `да` на включение generic host binding в тот же CR и один независимый review изменённого контракта. До валидного review новой ревизии код не меняется. Исторические DRAFT/read-only/gate формулировки ниже не отменяют этих решений; они не являются текущим статусом допуска. Доказательства и ограничения исходного CR сохраняются.

Граница допуска: поправка разрешает подготовить и проверить изменённый контракт. Она не выдаёт разрешения на no-model E1/E2 campaign, transport pilot, native/model sessions, микрофон, передачу речи, paid resources, production, новые ключи/доступы, установку, push или merge. Единственный writer — текущий Hermes интегратор. Bimo остаётся владельцем ASR/retrieval/GPU, kit и другие worktree не изменяются. Бюджеты parent CR, попытки и spent effects не обнуляются. Разрешён ровно один новый независимый read-only review этого изменения; корректирующие model-проходы этим не разрешаются.

### Требуемая generic привязка

1. Разделить объявление и запуск сервиса: plugin `register(ctx)` объявляет factory без создания journal, owner lock, Job или worker. Только owning serving host запускает service после owning-profile discovery и до admission новых conversations. Diagnostic/discovery команды не запускают service. Startup failure закрывает admission соответствующего consumer и оставляет UNAVAILABLE/HOLD; logger/fail-open hook не заменяет эту гарантию.
2. Host передаёт immutable profile home, namespace и lifetime generation. Один service/port на profile+namespace; повторное подключение не создаёт второго owner. Явные profile scopes сохраняются в background callbacks, shutdown и thread hops. `plugins.isolation: host` не обходится; невозможность передать live in-process port означает отказ. Не добавлять daemon, IPC, queue, HTTP, scheduler или отдельный Hermes runtime.
3. Request authority приходит только из host-owned проверенного источника и связывается с immutable profile/session/request identity, approved payload digest, contract и grant. Plugin получает её в ограниченном request scope; вне него, после завершения, при mismatch/expiry или отсутствии issuer — None/HOLD. Session/call IDs, cwd, текст запроса, config boolean, `confirmed: true` или согласие на разработку не создают execution grant. Partial ASR не является командой. Передача context через tool args запрещена.
4. До native admission необходимо определить конкретный approval source и проверить его связь с exact model/environment/attempt permit. Пока он не закреплён, native admission остаётся HOLD. Generic binding не подделывает grant, не создаёт новый доступ и не выдаёт этот недостающий источник за работающую интеграцию. Реализация generic части может оставаться inert при отсутствии источника; недостижимый placeholder нельзя предъявлять как завершённый serving transport.
5. При shutdown/unload/force rediscovery host первым закрывает admission, затем выполняет bounded drain и ledger cleanup в owning profile. Каждый успешно приобретённый ресурс получает cleanup до публикации service; abandoned loader не публикует live service. Ошибки cleanup не дают cancel ACK. Старое поколение service и late request callbacks не получают новый dispatch slot.
6. Единственный конкретный consumer — существующий `symphony_pm`: loaded owning-profile module → existing adapter → один Port/LifecycleOwner → существующий CodexDriver с attempt-bound runner. Factory declaration не меняет текущий prompt/tool prefix; активация поверхности — до новых conversations. Сбой service отказывает внутри уже выбранного handler, без смены toolset посреди turn.

### Scope файлов и совместимость

Разрешённая область поправки: generic plugin service/request-binding sibling modules в `hermes_cli`, узкие additive методы `PluginContext`/`PluginManager`, lifecycle call sites owning host в `tui_gateway` и `gateway`, consumer внутри существующего `plugins/symphony_pm`, соответствующие tests/docs. Новые API должны иметь первого consumer и реальную host call site. Названия `plugin_services.py` и request-binding module — варианты размещения, не утверждённые сигнатуры. Конкретные пути выбираются по area AGENTS до редактирования. Не hardcode-ить Symphony в core; не менять kit driver/frozen S2, общую adapter schema, профили или config оператора. Существующие plugins без новой регистрации сохраняют поведение.

### Критерии и неизвестное

Новые критерии: discovery без serving не создаёт ресурсы; повторный start не дублирует owner; профили не разделяют authority/handles; request binding не переживает окончание запроса и не даёт authority постороннему потоку или detached task; missing/expired/mismatched grant отказывает; startup/reload/shutdown закрывают admission; loader abandonment не публикует service; pinned host isolation не обходится; старые plugins совместимы; mid-session prompt/tool prefix сохраняется. Далее действуют исходные journal/launch/cancel crash criteria. Выполнение этих проверок NOT_RUN и требует отдельного допуска на bounded no-model campaign, а не нового model smoke.

Материальное UNKNOWN: конкретный production approval source и его доставка из UI/native permit в request binding пока не закреплены. Это блокирует native admission и сквозную приёмку, но не позволяет заменять их синтетическим context. OS observer/Job lifetime/crash recovery этой поправкой не доказаны. Общий cancel/release остаётся HOLD; ASR cancel/остановка записи не доказывают остановку worker. Rollback сохраняет журнал, identity, effects и reservations; старую схему поверх spent intent не восстанавливать.

## Исходная ревизия: исторический статус и сохранённая спецификация

Статус: DRAFT, предложение не принято. Реализация не разрешена.
Профиль: Standard + Hardened extension для durable dispatch/OS lifetime; текущий evidence cap E0 (чтение и документы). Будущие no-model E1/E2 требуют отдельного допуска на выполнение. Владелец решения — пользователь. Дельта продолжает ingress/cancel CR, не обнуляет прежний proof budget. Два оставшихся рубежа: независимый Hygiene + ok на реализацию; отдельное точное разрешение transport pilot.

## Контекст и источники

Фактическая база: `3feceb1f7c0b1e37a6e5a6ce00ffa6fa6aa1e02b`, ветка `codex/hermes-symphony-cancel-correction-20261106`. Parent — ожидаемый `fda3bda4bffece8bc3cca7e11be8dd8e72eef91d`; проверенная corrective implementation — `b766aecb8524a5c0b78197cda7a144473af4fd07`. Новый HEAD записал controller-restart evidence. Шесть прежних tracked modifications и untracked материалы сохраняются; другой worktree на `369a012c` не меняется.

Канонические локаторы: `.artifacts/controller-restart-20261106/{REPORT.md,verification.json,durable-readback.json,package-hashes.json}`. Предыдущий `docs/audits/CR-hermes-symphony-ingress-offline-20261106/cancel-correction-handoff.md` — исторический handoff: его NOT_RUN для C1→C2 устарел, прочие ограничения не снимаются автоматически.

Доказано записанным пакетом: 22 PASS/0 FAIL, synthetic Python root/child/conhost; C1 10836 → C2 23496; epoch 1→2; один native cancel; C2 receiver=None, новый worker отсутствует. Parent observer выжил с query-only handles. Durable readback сохраняет DISPATCH_INTENT, dispatch_epoch=1, proof/proof_epoch=NULL. Общий cancel HOLD, резервация POSSIBLY_CONSUMED_HELD. Это историческое runtime evidence, не новый прогон и не qualification serving path.

Актуальный исполнитель прочитан без запуска: `D:/AI/worktrees/kit-wave1-integration-20261031`, HEAD `048d446ba484d5d3ca8b052210dbeed3e18d7fb8`, VERSION 0.45.4; `runtimes/codex/orchestration/driver.py`, `tools/orchestration/drivers.py`. Это отдельный kit worktree, не установленный plugin cache 0.16.8. В нём ничего не изменяется.

## Цель и область

Предлагается сделать жизненный цикл локального исполнения явным в serving process: один owner/port на обслуживаемый profile + namespace; восстановление раньше admission; одна durable identity и один cancel effect для PM_CANCEL/TIMEOUT/EMERGENCY_CLEANUP. Никакой новой очереди или HTTP-сервера.

Текущий допуск: чтение, анализ, CR и gate artifacts. Новые модели, Codex workers, live-прогоны, production, ключи, постоянные доступы, push и merge запрещены. До отдельного ok реализация этой дельты не начинается. Успешный smoke не повторять. Настоящий pilot требует отдельного согласования модели, среды и точного количества attempts.

## Что переиспользовать, что отсутствует

| Компонент | Переиспользование / необходимая дельта |
|---|---|
| `tools/symphony_pm_adapter.py` | Сохранить Protocol submit/status/cancel, digest binding, receipts, TrustedPmContextV1 и fail-closed UNAVAILABLE/HOLD. Authority только из request-scoped trusted source. |
| `plugins/symphony_pm/__init__.py` | Сохранить wire/unwire через loaded owning-profile module. Wire до начала conversations; смена registry generation не разрешает менять tool surface посреди разговора. При аварии port отказывает внутри handler, не перевязывает toolset каждого turn. |
| `tests/fixtures/symphony_pm_ingress/owner.py` | Переиспользовать семантику SQLite transaction/dedupe/effect key и проверки token. Не импортировать fixture в production. `claim()` сегодня проверяет lease, но не OS death; fake admit/Observer/FencedSink не пригодны для serving. Нужен project-owned production journal module с совместимой схемой и явной миграцией. |
| `.artifacts/live-smoke-20261106/cancel_control.py` | Сохранить intent-before-effect, spent-on-exception, один permit/effect key, reason/readback отдельно, HOLD. Убрать зависимость от tests fixture и прямого приватного DB API через production journal contract. |
| `smoke.py`: LiveOwner/LivePort/Attempt/NativeRunner | Только проверенный образец. Dict attempts, mutable runner.active, synthetic context/current payload, два campaign attempts, globals и resume_unstarted не являются transport. Нужны восстановление из journal, request-local binding и сериализация admission без новой очереди. |
| `winjob.py` и controller_restart_probe.py | Образцы Windows Job/suspended spawn/query readback; sandbox parent — не production observer. Требуются явный lifecycle и crash matrix. |
| CodexDriver/CliDriver | Сохранить argv, schema projection/result collection, bypass gate, model/effort/budget validation и runner injection (`argv,cwd,timeout,stdin_text → CommandResult`). Runner не является scheduler или durable cancel owner. Не вызывать readiness/probe/launch сейчас. |

## Предлагаемый минимальный дизайн (PROPOSED)

1. Serving bootstrap создаёт LifecycleOwner и один Port до приёма запросов. Profile scope связывается явно; DB path выводится из переданного profile home, не из module-global environment. Lock ключуется canonical profile/namespace. При host-isolation, где live port не передаётся, запуск отказывает; обход pinned isolation запрещён. Сам plugin не патчит Hermes core. Точное место host bootstrap должно быть выбрано при реализации из существующих lifecycle seams; отсутствие подходящей seam — HOLD с отдельной generic extension, не hardcode Symphony в core.
2. Внутри serving process LifecycleOwner владеет Job Object, управляющими process handles и receiver. Controller — заменяемая логическая writer-сессия. Observer — отдельный от controller компонент с query-only non-inheritable handles; он не держит terminate capability. Защита от гибели всего serving process — Windows Job kill-on-close. Здесь намеренно не обещается переживание observer/serving crash с живым worker: при утрате последнего Job handle kernel может остановить дерево, это отдельный fail-safe, не acknowledged cancel. Отдельный daemon/IPC сейчас не добавляется.
3. Journal хранит versioned owner incarnation, profile/namespace, host boot identity, serving/controller PID + exact OS creation identity, writer epoch, observer generation/health; job/revision/request digest/attempt/runtime/session; named Job identity и root/известные members PID+creation; launch intent, native binding и cancel effect. Числовые handle values не durable identity. После reboot или несовпадения host generation attach запрещён; запись остаётся UNKNOWN/HOLD. Job name с уникальным attempt сам по себе недостаточен для доверия.
4. Admission: exclusive owner lock → проверка trusted grant/contract/profile → восстановление незавершённых launches/effects → healthy observer handshake → durable launch ticket → suspended create → assign Job → exact binding + observer coverage durable commit → resume. Непривязанный launch intent после сбоя не возобновляется по отсутствию command.json: возможен уже созданный worker. Он блокирует новый attempt до достаточной сверки. Ошибка до resume допускает только отдельно определённый pre-admission rollback собственных exact handles; ошибка storage оставляет неопределённость.
5. Fencing: lease expiry недостаточно для takeover. В том же serving process lifecycle lock сериализует смену writer и native entry. При новом serving process требуются exclusive OS lock и exact predecessor process death (retained handle либо freshly verified PID/start + wait); отсутствие доступа/identity mismatch означает HOLD. В одной транзакции записывается новый epoch; старый token отвергается каждым write/permit. Native receiver под тем же lifecycle lock повторно проверяет incarnation/epoch/target, тратит send slot до native entry. Так закрывается race между authorize и эффектом. Wall clock lease не доказывает death.
6. Recovery: сначала readback, затем разрешение работы. Spent DISPATCH_INTENT/UNKNOWN никогда не создаёт новый receiver/send slot или launch, не повторяет native cancel, сохраняет effect key и исходный dispatch_epoch. Новый controller получает reconcile-only доступ к такому attempt. Fresh proof связывается с текущими writer epoch/observer generation, но исходный target не переписывается. Без непустого exact process evidence, Job coverage и свежести локальный stop не утверждается; missing Job/access denied/чужой PID не равны terminal proof.
7. Потеря observer: немедленно закрыть admission и native dispatch. Управляющие handles остаются у LifecycleOwner; восстановленный observer сначала заново подтверждает exact identities и полное известное дерево. Уже начатая native entry учитывается как spent. При гибели serving process новый owner не наследует handles: заново открывает только query handles после доказательства predecessor death. Если объект исчез или процесс уже переиспользован — UNKNOWN, без auto-relaunch. UNKNOWN не снимает reservation; локальный stop даёт только LOCAL_STOPPED_HOLD, не external settlement.
8. Transport: loaded plugin wire(Port, request-local trusted context source) → adapter → Port → LifecycleOwner journal/admission → существующий CodexDriver с привязанным к attempt runner. Первый вариант сериализует admission и отказывает concurrent launch как busy, не ставит очередь. Runner не использует общий mutable active. PM cancel/watchdog/cleanup идут в один coordinator. Status строится из durable state и OS readback, не из наличия thread в dict. Дедуплицированный submit возвращает прежний job, не вызывает driver повторно. Frozen S2 остаётся закрытым reference snapshot; flags не превращают его в scheduler.

## Acceptance и отдельная no-model кампания (NOT_RUN)

После отдельного разрешения: выделенные temp HERMES_HOME/runtime/journal; synthetic Python worker, никаких CLI probes или Codex/model calls. Тесты через `scripts/run_tests.sh`, после чтения area AGENTS; Windows свойства на Windows. Один bounded campaign, затем только проверки, затронутые найденными дефектами. Исторический успешный smoke не повторяется.

| Сценарий | Критерий приёмки |
|---|---|
| stale writer и race native entry/takeover | Старый token не пишет и не посылает native после смены epoch; зависший живой predecessor не вытесняется lease timeout; deterministic barrier проверяет race. |
| чужой PID / тот же PID, иной start / чужой Job | Attach и cancel отвергнуты; чужие процессы не изменены; admission закрыт. |
| controller loss при живом lifecycle/observer | Epoch меняется один раз после fencing; target/effect прежние; ни нового worker, ни повторного cancel. |
| потеря query observer до эффекта и после spent intent | Admission/dispatch закрыты, UNKNOWN/HOLD; новый observer не выпускает sender для spent attempt; fresh readback привязан к новой generation. |
| потеря всего serving owner/observer | Отдельный synthetic process crash; новый owner открывает journal без inherited handles. Kernel stop маркируется отдельно. Исчезновение Job/недостаточная coverage не даёт ложный PASS или release. |
| cuts before native / after effect before ack / lost reply | Durable intent не переиздаётся даже если вызова могло не быть. Счётчик native entry ≤1, число launches=1, тот же attempt/effect, reservation held. |
| lost submit reply / конкурентный submit / storage error | Один durable launch ticket; replay возвращает прежний результат; unbound intent блокирует повтор. Ошибка commit не разрешает resume/новый attempt. |
| реальный plugin discovery в temp profile, injected no-model runner | До wire UNAVAILABLE, после wire один port call; trusted context не берётся из tool args; два профиля не разделяют authority/handles; mid-session prompt/tool prefix сохраняется. |
| shutdown и ошибки cleanup | Admission закрыт первым; bounded drain и освобождение всех собственных handles продолжаются при ошибках; закрытие handles не называется cancel ack. |

Power loss, настоящий Codex restart-stop, remote inference, billing и внешние последствия остаются UNKNOWN. Их отсутствие не блокирует подготовку дизайна, но блокирует утверждение общего cancel/release и production readiness.

## Зависимости, риски и rollback

Затронуты: production journal/lifecycle/port/runner в отдельном project-owned transport package, plugin bootstrap, тесты plugin/adapter, документация handoff и state schema. Конкретные новые пути выбираются после ok; shared adapter contract сохраняется. Kit driver и frozen S2 не меняются. Не добавлять core tool, ENV config, queue, HTTP или scheduler facade.

BLOCKING для рабочего транспорта: нет production journal без fixture imports, нет lifetime host/bootstrap, receiver-side atomic fencing, durable launch recovery, request-scoped trusted context и observer health admission. Fixture lease и smoke dict допускают split-brain/потерю binding; OS-only readback не устанавливает внешние последствия. Дешёвое достаточное доказательство дельты — описанная synthetic crash matrix, не новый model smoke. Риски storage/create gap и observer loss ограничиваются fail-closed HOLD, не обещанием exactly-once запуска при power loss.

Rollback: отключить новые admissions/transport до нового сеанса, unwire в безопасной lifecycle точке, дождаться bounded drain собственных attempts; cleanup использует прежний coordinator, не прямой второй cancel. Сохранить journal, identities/effects/reservations и evidence. Не откатывать DB snapshot поверх spent intent и не downgrade-ить несовместимую схему. Старую версию запускать только если она понимает journal либо inert; ручная сверка unresolved attempts обязательна. Чужие файлы/процессы не трогать.

## Gate и граница утверждений

Подготовлено: предложение архитектуры и критерии. Реализация/проверки новой дельты — NOT_RUN. Независимый Hygiene reviewer не запускался: текущий допуск запрещает новые модели/Codex workers. Детерминированные prefilter/Context Pack находятся в `.artifacts/serving-lifecycle-cr-20261106/`. Пока нет independent report + validate-report + status READY, документ не предъявляется как готовый к ok. Требуется отдельный fresh-session/external-review в разрешённом контексте; самопроверка не заменяет gate.
