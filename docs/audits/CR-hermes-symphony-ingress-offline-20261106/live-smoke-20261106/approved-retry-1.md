# Hermes → Symphony / Codex: утверждённый повтор и отмена

Ограниченный live sandbox smoke выполнен. **Офлайн-контракт — PASS 144/144; живой read-only результат — PASS; локальная отмена/дедупликация — PASS. Общая приёмка отмены — HOLD**, поскольку external effects settlement и billing settlement остаются UNKNOWN. Резервация `POSSIBLY_CONSUMED_HELD`; полный PASS не заявляется.

Авторизация расширена двумя явными указаниями: «Если ты исправил, то повторяй» и «Да, разрешаю третью для отмены». Выполнено **2 native attempts в этом допуске, 3 суммарно**, включая сохранённый прежний config failure. Automatic retries = 0. Модель/среда прежние: `gpt-6.1-sol`, High, текущий Windows, существующая авторизация Codex. Никаких новых ключей, постоянных доступов или сред.

Существующий `CodexDriver` переиспользован. Один plugin → один LivePort → один SQLite owner store `.artifacts/live-smoke-20261106/owner.sqlite`; namespace прежний, writer epoch **2**. Предшествующий owner lease завершён только после свежего exact-process / Job readback и завершения предыдущих controller invocations; новый token получен через существующий Owner.claim. Старая failed attempt и evidence не переписаны. Новые ticket entries уникальны, старые ticket/targets не сброшены; новая runnable очередь или HTTP server не созданы. Frozen S2 не вызван и не открыт флагами.

| Прогон | request | job | attempt | PID / creation FILETIME | Native thread |
|---|---|---|---|---|---|
| Read-only JSON | `smoke-approved-retry-1-request-1` | `job-c66d2f6997754d30823966e8feecf42f` | `attempt-dfc5e5917d5b43f884d93fc8195ebdc1` | `23172` / `134384574108811070` | `01a1b1c4-90eb-7c63-bcf2-3ec6c7392579` |
| Cancel | `smoke-approved-retry-1-request-2` | `job-c874f68244cc4a4d9d40669cd054c880` | `attempt-426b7b910de94ec3b1856368c9322b40` | `26072` / `134384574242021339` | `01a1b1c4-c4ed-7ba2-800f-efa6d3c78949` |

Полные job revisions/request digests/target bindings — `attempt-*/native-target.json`, `first-verdict.json`, `cancel-verdict.json`, `ledger-readback.json`. Для каждого нового job — ровно один `NATIVE_STARTED`, один command.json и один launches entry. Read-only job имеет ровно один `thread.started`, `turn.started`, `turn.completed`; exit **0**.

Сохранённый результат:

```json
{"marker": "synthetic-95fc1f9965664af6866ef6bc3346dc68", "value": 17, "sum": 10}
```

JSON проверен равенством с отдельным случайным marker и значениями в synthetic `workspace-1/input.json`; задача прочитала файл реальной shell-командой. Результат сохранён в `attempt-1/result.json` и SQLite terminal result. Дубликат submit вернул тот же job и не запустил worker. Первый shell выдал диагностическое сообщение при попытке загрузить пользовательский PowerShell profile в constrained language; сама read-only команда завершилась exit 0 и вернула ожидаемые данные. Диагностика сохранена в stdout.jsonl; bypass/elevation для shell не применялись. Второй worker использовал `-NoProfile`.

До второго запуска автоматически проверены JSON, native terminal и query-only OS terminal readback первого. Вторая задача реально прочитала свой synthetic input, затем начала ограниченную 90-секундную wait-команду. Перед cancel отдельный observer увидел **7 active processes**: Codex, code-mode host, PowerShell и обычные потомки/console hosts. Есть native `item.started` для команды `SMOKE_CANCEL_READY` / `Start-Sleep -Seconds 90`; это подтверждение started command, не утверждение о полученном stdout marker (команда отменена до completed output).

Единственный native cancel primitive — **Windows TerminateJobObject**, не app-server `turn/interrupt` и не подтверждение остановки удалённой inference. Он отправлен **один раз** в exact-attempt Job Object после durable cancel intent:

- effect key: `36b51a3c1fb40fa8566f91e69d37cf999cc84939928ad8f927bf4d995485c6c4`;
- event time: `1793983835.7729657` UTC epoch seconds;
- cancel request `cancel-lost-response`: effect выполнен, ответ намеренно потерян на port seam → PM `UNKNOWN/HOLD`, `RESPONSE_LOST`;
- после reopening реальной БД и нового wire binding повтор с тем же request ID и с `cancel-redelivery-new-id` вернул **ALREADY_REQUESTED**;
- в БД один spent cancel effect ticket, в event journal один native dispatch; счётчик не сбрасывался при reopen;
- повтор submit второй задачи вернул тот же job; число launches осталось **3 суммарно**, новых запусков после восстановления нет;
- SQLite+port recovery выполнен при живом том же controller с сохранёнными OS handles; полный controller restart / power-loss recovery этим не доказан.

После native cancel driver завершился exit **123**. Независимый Python observer с query-only Job handle подтвердил **active=0**, пустой PID list, `tree_stopped=true`; все **9 сохранённых process handles** signaled. Root и обычные потомки в non-breakaway Job Object остановлены. Query performed после native cancel; observer не читает receipt stop flags и не обладает cancel capability. Полные before/after snapshots: `attempt-2/running-observer.json`, `attempt-2/terminal-observer.json`. OS proof сохранён до закрытия управляющего handle. Ноль active processes повторно прочитано при финальном cleanup обоих новых Jobs.

PM receipts всё ещё содержат `worker_stopped=false`, `verified_claims=[]`: отдельные OS proofs здесь сохраняются в acceptance evidence и не превращаются автоматически в четыре trusted PM claims. Это не отсутствие OS stop proof; в binding нет producer qualification для всех четырёх claims. `external_effects_settled` и `billing_settled` **UNKNOWN**, reservation удержана, поэтому overall cancel **HOLD**. Локальная остановка не означает доказанную остановку серверной inference или zero cost.

Версии: **Codex CLI 0.167.0**, kit **0.45.4**, kit HEAD `048d446ba484d5d3ca8b052210dbeed3e18d7fb8`, Hermes HEAD `c419cf7c864ac0ee511dbd9f7091a8bba0b567ee`; Python **3.14.9**, pytest **9.1.1**, psutil **7.2.2**; PM contract `hermes-pm-ingress/v1`. preflight readiness — ready. SHA256 выбранного существующего driver `1b88924648b442675749890b726668790f8c7864707823e06f543acdd0cbe176`. Новый argv не содержит отвергнутых `model_providers.openai.*` overrides. Внутренние HTTP/SSE retries не квалифицированы; application/native automatic retries отсутствуют.

Точные выполненные команды (PowerShell, cwd Hermes worktree):

```powershell
$smokePython = 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe'
& $smokePython -B .artifacts/live-smoke-20261106/retry_campaign.py prepare
& $smokePython -B .artifacts/live-smoke-20261106/retry_campaign.py config-check
& $smokePython -B .artifacts/live-smoke-20261106/retry_campaign.py run
& $smokePython -B .artifacts/live-smoke-20261106/retry_campaign.py inspect
& $smokePython -B .artifacts/live-smoke-20261106/package_retry.py
```

Native run выполнен вне ограничений внешнего sandbox по действующему допуску; сам Codex shell оставался **read-only / Windows unelevated sandbox / approval never**, без bypass. Полные argv с точными путями, stdin prompt hashes и моделью/effort: `attempt-1/command.json`, `attempt-2/command.json`; исполнимое PowerShell-представление фактических argv — соответствующие `executed-command.txt`. Это архив, а не команда для повторного запуска; projected temporary schema удалена штатным driver.cleanup. Timeout 180 секунд не объявляется денежным лимитом и не используется как stop proof.

Usage первого успешного turn: `{"input_tokens": 39949, "cached_input_tokens": 19712, "cache_write_input_tokens": 0, "output_tokens": 149, "reasoning_output_tokens": 0}`. Наличие usage не устанавливает billing settlement. Cancelled turn не прислал turn.completed usage.

Проверки: source hashes совпали и до, и после; baseline tracked status и HEAD прежние; frozen S2 **21/21** sizes/hashes. Поэтому результаты предыдущего canonical targeted campaign **144/144** применимы к неизменившимся adapter/plugin/reference owner/tests. Новая task-local orchestration проверена двумя настоящими worker attempts, потерей ответа/replay, отдельным OS observer и Ruff. Полный repo aggregate повторно не запускался и не объявляется зелёным. Рабочие проекты в задания не передавались; Linear writes, push, merge, публикация и production отсутствуют. Synthetic workspaces/results/state сохранены.

Достаточный ограниченный smoke завершён. Следующие native attempts этим допуском не разрешены; limit=3 суммарно исчерпан. Полная production integration, durable handle service, полный controller restart, квалификация evidence producer, внешних эффектов и billing остаются отдельной работой.
