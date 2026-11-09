# Координатору: serving CODE и план следующего шага

Дата 2026-11-09. База перед публикацией `8169f1b66a3a7c6844e3347c89f5cfa82edd4483`; итоговый commit определяется Git commit, содержащим этот файл. Ветка `codex/hermes-symphony-cancel-correction-20261106`, fork `mywondvision/hermes-agent`. Это ветка для проверки, не deployment или production-ready transport.

## Что передаётся

Generic host-owned service lifecycle в `hermes_cli/plugin_services.py`, request-scoped authority в `hermes_cli/plugin_request_binding.py`, additive host bootstrap/shutdown seams в gateway/TUI/HTTP и первый consumer `plugins/symphony_pm/service.py`. Plugin discovery само по себе не запускает transport; host изоляция не обходится. Без явно заданных bindings tool остаётся UNAVAILABLE.

Project-owned пакет `plugins/symphony_pm/transport/`: versioned SQLite journal с explicit legacy quarantine, exact immutable permit/launch/native identities, один coordinator PM_CANCEL/TIMEOUT/EMERGENCY_CLEANUP, attempt-bound runner, conservative recovery и приватные результаты. Approval bridge использует существующий `RegisteredApprovalTransport`; getter не показывает UI и не выдаёт grant из ASR/model text.

Канонический CR: `docs/change-requests/CR-hermes-symphony-serving-lifecycle-20261106.md`. Hygiene PASS относится к нормализованной ревизии `d408f7b5b619e13ab13fd113b428b7e5633399719d0b7732df7fa66a8d2d691d`, не к квалификации реализации. Детальные отчёты в этой директории: `CODE-HOST-BINDING-20261109.md`, `CODE-JOURNAL-LIFECYCLE-20261109.md`. Исторические NOT_RUN/HYGIENE_PENDING в сохранённых snapshots не являются текущим итогом; приоритет у последующих квитанций и этих отчётов.

## Проверки и пределы вывода

| Проход | Результат | Команда / время |
|---|---|---|
| Generic binding, 8 файлов | 114 PASS / 0 FAIL, без повторов | `python -X utf8 -I -B .artifacts/serving-lifecycle-implementation-20261109/code/run-contract-campaign.py`; 135.703 s (runner 132.1 s) |
| Journal/lifecycle/permit/bootstrap, 4 файла | 24 PASS / 0 FAIL, без повторов | `python -X utf8 -I -B .artifacts/serving-lifecycle-implementation-20261109/transport/run-transport-campaign.py`; 89.859 s (runner 82.8 s) |

Оба выполнены штатным `scripts/run_tests.sh -j 1 --file-retries 0 --file-timeout 60`, с cap 600 s, существующим test interpreter, выделенными HOME/runtime/TEMP, scrubbed env/offline/lazy installs disabled. Это fixture/no-model проверки; subprocesses принадлежат pytest, не native task workers. Во втором проходе все 15 проверенных source hashes совпали после выполнения. Четыре прежних serving-теста входят в оба прохода: результаты не складывать в 138 уникальных проверок. Старый manifest фиксирует старый service/test source; его несовпадения явно описаны в новом manifest.

В `.artifacts/serving-lifecycle-implementation-20261109/{code,transport}/` публикуются команды, bounded approvals, source manifests, журналы и proof/результаты; временные HOME/БД/cache/runtime не входят в commit. Оба campaign grants израсходованы; повтор ради уже пройденных свойств не нужен. Ветка не имеет утверждения полного CI/lint PASS. Общий worktree whitespace check находил прежний trailing whitespace в `events.jsonl`; этот посторонний evidence не включён в commit и не исправлялся.

Вне этого review bundle остаётся прежний локальный `.artifacts/frozen-s2/` source snapshot, который используют отдельные regression cases 8-файловой кампании: manifest `p0=cb4e5f37f56e9dac062506a566d7cfc0febf70d4a5556a56a76af28f01ae5ece`, `candidate=7aa4321ce3e7940b7d2191979a94be88d95f3ce79fe429631b4e76cdacdf88fa`. Его dirty manifest и untracked source не включены в этот commit; для воспроизведения этих исторических cases нужна отдельно сверенная исходная fixture по manifest, а не текущий kit HEAD. Также не публикуется установленный test interpreter. Это зависимости воспроизведения, не отсутствующие native runtime permits.

## Текущие статусы

| Компонент | Статус |
|---|---|
| Installed isolated S3 | Source `c419cf7c864ac0ee511dbd9f7091a8bba0b567ee`; новая Symphony CODE туда не установлена |
| Global voice hotkey | NOT_IMPLEMENTED. Ctrl+B при фокусе и Quick Entry text не закрывают критерий |
| Живая русская речь → ответ | NOT_RUN; microphone/voice-transfer и answer model/provider не согласованы этим commit |
| Новый Hermes → Symphony native task | NOT_RUN; qualified native providers/launcher и exact execution permit отсутствуют |
| Native/global cancel, release | HOLD. Fixture cancel, ASR cancel и остановка записи не доказывают остановку worker; external/billing UNKNOWN |
| OpenWhispr | 1.10.1 сохранён, равноценная live-проверка ещё не выполнена |

## Предлагаемый следующий шаг (PROPOSED)

1. Проверить переданный CODE, особенно lifetime cleanup/startup failure, authority thread/task/profile binding, explicit schema migration, intent-before-effect, потерянный ACK и отсутствие повторного send slot. Native scope не открывать по результату fixture PASS. Любой обнаруженный дефект оформить одним конкретным checkpoint; parent proof/correction budgets не обнулять.
2. В рамках исходного serving CODE scope реализовать конкретные Windows ownership guard/query observer/native receiver и launcher на существующем CodexDriver. Предлагаемые новые файлы внутри того же `plugins/symphony_pm/transport/`: `windows_ownership.py`, `windows_observer.py`, `windows_receiver.py`, `codex_launcher.py`; это план, файлов пока нет. Нужно настоящее OS ownership/predecessor death, boot/PID+creation/Job identity, query-only non-inheritable handles, suspended create + durable binding до resume, receiver-side atomic fencing и kill-on-close. Политика executable/machine lookup — через `hermes_platform`. Существующий kit driver/его schema/model/effort/budget/bypass/result checks не дублировать и не менять.
3. Подготовить конечную targeted native no-model crash matrix на собственных synthetic процессах; выполнить только после отдельного допуска. Исторический C1→C2/model smoke и аренду не повторять. Даже kernel fail-safe не является acknowledged cancel или billing settlement. Нужны собственные квитанции по каждой границе отказа, без нового dispatcher/queue/HTTP/daemon.
4. После runtime qualification подготовить минимальную глобальную voice activation в существующий Desktop/нужную Hermes session; проектировать через существующий voice ingress, без вставки в активное случайное окно и двойного STT. Desktop voice implementation/установка и живой smoke не входят в этот commit. Сначала cross-window RU reply без внешних действий: числа, отрицания, тишина/шум, cancel, повторная клавиша, смена окна, latency. Потом одна разрешённая task с точными моделью/effort/средой/attempt permit.

## Владение и внешние зависимости

Единственный интегратор/writer Hermes → Symphony владеет перечисленным transport/host CODE. Bimo остаётся владельцем ASR/retrieval/GPU и своих файлов; отдельный Hermes/runtime/Symphony там не создаётся. Переданный read-only Bimo intake — отдельный контур, его файлы в этот commit не переносятся. До live backend handoff запросить/закрепить voice fixtures + oracle, audio provenance, exact FINAL uid/empty-final/error/WorkerRejected/length contract, cancel acknowledgement vs actual remote stop, retrieval bundle и receipts ресурсов/начислений. Синтетические RU 9/20 и EN 13/20 не подтверждают качество живого голоса.

Не переносить локальное состояние и единственный STOP на удалённый host. При потере Bimo должны оставаться локальные STOP и текстовый ввод. Микрофон, внешняя передача реального голоса, платные ресурсы и production требуют соответствующего точного допуска; новых ключей/доступов не создавать.

## Rollback и публикация

Новая CODE не установлена: до квалификации не включать binding/admissions. При будущем запуске сохранять journal/identities/spent effects/reservations; не восстанавливать DB snapshot поверх uncertain intent. Закрывать только собственные handles через owner lifecycle; чужие worktree/процессы не менять.

Пользователь явно запросил commit и push для передачи координатору. Публикация этого review bundle не является разрешением merge/deploy, native/model/mic/paid запуска либо нового review-модельного прохода.
