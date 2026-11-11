# CR: Hermes → Symphony serving lifecycle — продолжение 2026-11-11

context_hygiene: PASS rev=7c316c89f571 (проверена приватная полная редакция; эта копия отредактирована для публикации)

Статус: DRAFT, ожидает решения владельца. Продолжает линию `CR-hermes-symphony-serving-lifecycle-20261106.md`
(ревизия `d408f7b5b619…`); новый CR ради обнуления proof/correction budget не создаётся. Профиль: Standard +
Hardened extension (durable dispatch / OS lifetime). Владелец решения — пользователь.

## Контекст и проверенное исходное состояние

Источник задачи: отчёт `docs/audits/CR-hermes-symphony-serving-lifecycle-20261106/REPORT-20261110.md`
(опубликован в fork как commit `f0d20a8d86bb94dba599aa427e981bab5185c355`; по квитанции публикации
`.artifacts/publish-serving-report-20261110/safe-push-result.json` — один файл, parent `16ea4b7a`; remote
readback 2026-11-11 показывает ветку на этом commit; самого объекта в локальной object DB нет).

По отчёту: production create→Job assignment gap сохраняется: смерть owner до assignment может оставить
suspended orphan. Реальный CodexDriver/model stop, production approval source и qualified driver/environment
bindings не приняты. Все описанные grants израсходованы.

Read-only preflight 2026-11-11:

- Worktree `<worktree>`, ветка `codex/hermes-symphony-cancel-correction-20261106`,
  HEAD `16ea4b7abbb2541c61c7059f3bde6b3c784d261a`, index пуст. `f0d20a8d` в локальной object DB отсутствует.
- Исходная сессия: сессия исполнителя (идентификатор скрыт) (последняя запись 2026-11-10), сейчас
  не активна. Второй worktree репозитория чист и не затрагивается. Lock-файлов нет.
- Все 26 закреплённых source/report hashes совпадают; manifest SHA256 `b3291f80…70ba`, verification `af725d00…2fd4`.
- Локально вне Git: 3 изменённых tracked source (`journal.py`, `lifecycle.py`, `symphony_serving_support.py`),
  6 прежних tracked evidence-модификаций, 33 untracked вне `.artifacts` (Windows transport, тесты, отчёты).
- Сохранение: byte-exact архивы и manifest в `<preserve>/`, локальный
  ref `refs/preserve/lifecycle-cancel-20261111` (никогда не публикуется). HEAD и index не менялись.
- Посторонний потребитель: прямой CLI-путь голосового контура запускает интерпретатор из
  `.artifacts/s4-global-voice-20261106/prepared/`. Этот каталог и рабочее дерево прямого CLI-пути не
  изменяются, не перемещаются и не блокируются.

## Директивы владельца

Дословные директивы и решения владельца хранятся в приватной полной версии; для воспроизведения не нужны.

## Цель

Закрыть оставшиеся пробелы lifecycle без снятия HOLD: production create → Job assignment gap, квалификацию
owner/driver/issuer, восстановление после сбоев; выпустить воспроизводимый переносимый пакет, независимое
ревью и точный контракт для голосового интегратора.

## Scope

Технические решения пунктов 1–7 — предложение исполнителя, а не принятое решение, до `ok` владельца.

1. **Атомарное создание в Job.** `windows_api.create_suspended` создаёт процесс сразу внутри kill-on-close Job
   через `STARTUPINFOEX` + `PROC_THREAD_ATTRIBUTE_JOB_LIST` (вместе с `HANDLE_LIST`), `CREATE_SUSPENDED`;
   отдельный `AssignProcessToJobObject` и pre-admission `TerminateProcess` из рабочего пути уходят. После
   создания — обязательный readback `IsProcessInJob`; несоответствие = отказ и закрытие собственных handles.
   Имя Job выводится детерминированно из уже durable ticket/attempt (без изменения схемы journal), чтобы
   новый owner мог выполнить сверку. `CREATE_INTENT` по-прежнему записывается до входа в ОС.
2. **Восстановление.** Матрица для нового owner по состояниям `LAUNCH_INTENT / CREATE_INTENT / BOUND /
   RESUME_INTENT / DISPATCH_INTENT`: сначала доказанная смерть predecessor и boot identity, затем query-only
   readback; результат сверки записывается, но spent slot не возвращается, повторного create/resume/cancel и
   нового receiver нет, reservation при UNKNOWN остаётся удержанной. Отсутствие Job — отрицательное
   свидетельство для записи сверки, не terminal proof и не release.
3. **Квалификация owner/driver/issuer.** Host-side сборка `PreparedCodexAttempt` и неизменяемая запись
   квалификации (kit revision, абсолютный путь и hash исполняемого файла, environment_ref, issuer refs,
   serving identity), проверяемая до выдачи `windows_transport`. Отсутствие любой части = UNAVAILABLE/HOLD.
   Проверяется только на fixtures; probe/readiness/launch настоящего CodexDriver не вызываются.
   Отдельно готовится execution pack (точные model/effort/environment/attempt) — документ-запрос на будущий
   допуск, сам допуском не являющийся.
4. **Проверки.** Затронутые fixture-тесты (journal, lifecycle, create fencing, serving, новые) и **один**
   synthetic native no-model проход затронутых Windows-тестов (`test_symphony_pm_windows_native.py`,
   `test_symphony_pm_windows_crash.py`): собственные synthetic Python-процессы, retries=0, выделенные
   HOME/runtime/TEMP, `scripts/run_tests.sh`. Уже доказанные кампании не повторяются. Один корректирующий
   fixture-проход разрешён только по конкретному найденному дефекту; повтор native-прохода требует нового
   допуска. Native no-model проход выполняется только по
   отдельному допуску владельца (решение 2 ниже): no-model campaign и operational permits остаются отдельными
   от разрешения на код.
5. **Пакет.** Локальный commit исходников, тестов и отчётов поверх `f0d20a8d`, предварительно полученного
   из fork обычным fetch (без force, история не переписывается); `git bundle` + source manifest + инструкция воспроизведения + версии test-окружения во
   внешнем каталоге пакета. Не входят: исходники Oct-31 (`tests/tools/symphony_pm_adapter.py`, `tools.zip`),
   сырые `.artifacts`, шесть прежних evidence-модификаций. Push в fork — отдельный выбор владельца (см. ниже).
6. **Независимое ревью.** Два read-only reviewer в чистом контексте: (a) fencing/recovery/native код,
   (b) контракт и пакет. Findings применяются одним checkpoint; ревью не снимает HOLD.
7. **Передача голосовому интегратору.** `VOICE-INTEGRATOR-CONTRACT-20261111.md`: точный контракт
   submit/status/cancel, перечень статусов и их допустимая трактовка, условия подключения, что запрещено
   утверждать пользователю, fallback (локальный STOP и текстовый ввод).

Исполнение: архитектор/координатор — основная сессия; ограниченные слайсы реализации и сборки пакета
делегируются субагентам на более слабых моделях, строго по одному writer в worktree; ревью — отдельные
субагенты без доступа на запись.

## Out of scope (остаётся HOLD / UNKNOWN)

Native/global cancel, release и настоящий stop Codex/model остаются HOLD до необходимого отдельного допуска и
доказательств; запуск настоящего CodexDriver и любых моделей; микрофон, передача живого голоса, платные
ресурсы; установка в S3, deploy, merge; изменение kit driver, frozen S2, Codex home, второго worktree, прямого
CLI-пути и его каталогов; новые ключи и доступы. Не добавлять daemon, IPC, queue, HTTP, scheduler; kit driver
и frozen S2 не менять.

UNKNOWN: конкретный production approval source и его доставка из UI/native permit в request binding не
закреплены; reboot, power-loss, remote inference, billing и внешние последствия остаются UNKNOWN.

## Acceptance

- Смерть owner в любой точке вокруг create не оставляет живого процесса вне Job: в native-тесте прежний
  случай `before_assign` завершается kernel-остановкой без страховочной cleanup стенда.
- После каждого сбоя: launches=1, native entry ≤1, тот же attempt/effect key, reservation удержана,
  новый sender/receiver не выдан; boot mismatch и identity mismatch дают HOLD.
- Неполная квалификация не позволяет собрать transport; fixture-тесты покрывают каждый отсутствующий элемент.
- Пакет воспроизводится из bundle + manifest на чистом checkout: совпадают hashes и результаты fixture-тестов.
- Отчёты reviewer сохранены; каждое finding закрыто либо явно отнесено к HOLD.
- Контракт для голосового интегратора не содержит утверждений сверх доказанного.

## Verification

`scripts/run_tests.sh -j 1 --file-retries 0` на перечисленных файлах; `python scripts/check` на изменённых
файлах; пересчёт source manifest до и после прохода; readback HEAD/index/ref после commit; проверка bundle
через `git bundle verify` и распаковку в отдельный каталог.

## Риски и rollback

- Ошибка в ctypes-структурах attribute list → отказ создания; покрыто readback и native-тестом; до прохода
  путь не активируется (transport не установлен).
- Вложенные Job: serving process сам может находиться в Job; Windows 10+ допускает вложение, отказ = Refused.
- Изменение имени Job затрагивает observer/tests — меняется одним checkpoint вместе с ними.
- Rollback: `git revert` нового commit либо возврат к `refs/preserve/lifecycle-cancel-20261111`. Rollback
  сохраняет journal, identities, effects и reservations; DB snapshot поверх spent/uncertain intent не
  восстанавливается.

## Решения, которые нужны от владельца

Дословные директивы и решения владельца хранятся в приватной полной версии; для воспроизведения не нужны.
