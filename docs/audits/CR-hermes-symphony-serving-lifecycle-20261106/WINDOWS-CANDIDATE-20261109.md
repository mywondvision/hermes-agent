# Windows serving lifecycle: кандидат CODE, native HOLD

Дата: 2026-11-09. Продолжение существующего CR по прямому поручению владельца; новая линия CR и второй orchestrator не создавались. Это E0 source checkpoint, не runtime qualification.

## Проверенная база и владение

Локальные branch и HEAD проверены заново: `codex/hermes-symphony-cancel-correction-20261106`, `16ea4b7abbb2541c61c7059f3bde6b3c784d261a`. GitHub connector разрешил имя этой ветки в тот же SHA. Remote origin остаётся NousResearch; публикация не выполнялась. Второй worktree остаётся на `<other-worktree>`. Kit прочитан без изменения: `048d446ba484d5d3ca8b052210dbeed3e18d7fb8`.

Handoff прочитан через GitHub по pinned SHA и локально; актуальный CR содержит PASS normalized revision `d408f7b5b619e13ab13fd113b428b7e5633399719d0b7732df7fa66a8d2d691d`. Этот PASS относится к CR, не к новому CODE.

Активные задачи проверены. Голосовой интегратор `<session>` подтвердил отсутствие своего параллельного writer и передал актуальные pins. Текущая сессия — единственный writer. Два независимых субагента выполняли только чтение и ревью, без записи, тестов или native workers. Их замечания и исправления перечислены ниже. Шесть прежних tracked modifications и прежние untracked артефакты не редактировались; индекс не использовался.

## Подготовленная реализация

- `windows_ownership.py`: exclusive non-inheritable file handle на canonical profile/namespace; PID+creation, boot GUID, incarnation; проверка смерти predecessor вместо lease takeover. Boot GUID вынесен в `hermes_platform/host/windows_boot.py`, неподдерживаемый native query отказывает.
- `windows_observer.py`: query/synchronize non-inheritable process handles; временные query Job handles закрываются после каждого запроса; Job проверяется через root membership и exact creation. Пропавший Job/PID либо неполная coverage означает UNKNOWN, не stopped proof.
- `windows_api.py`, `windows_receiver.py`: retained primary thread, suspended native create, Job без breakaway с kill-on-close, binding до resume, локальные одноразовые resume/cancel slots. Explicit child environment и только разрешённые stdin/stdout/stderr handles; private capture, ограниченный wait, capture cap с входом в прежний cancel coordinator. Никакого shell fallback для CMD/BAT.
- `codex_launcher.py`: существующий kit CodexDriver/PeerPack, cached qualified probe, exact model/effort/workspace/checkpoint/environment, attempt-bound runner, штатные schema projection/budget/bypass/result collector плюс обязательный canonical result validator. Executable проверяется через hermes_platform; новый probe не вызывается.
- `windows_bootstrap.py`: явная сборка через прежний TransportBootstrap/ServiceHost, с profile+namespace binding environment и launcher. Автоматической регистрации/активации нет. Конкретные approved host objects и production issuer остаются обязательными; их отсутствие не заменено фикстурами.

Независимое ревью нашло replay defect базового кода: ошибка create либо cut до binding оставляли повторно используемый LAUNCH_INTENT. Исправлено durable CAS `LAUNCH_INTENT → CREATE_INTENT` до native entry. Это дополнительное состояние существующей TEXT state колонки; schema layout не меняется, существующие записи не переписываются. Старый intent не объявляется безопасным для retry. Unfinished launch продолжает блокировать следующий attempt.

Resume/cancel теперь принимают receiver-side authorize callback. Journal держит writer transaction через native entry и проверяет epoch, observer generation/health, target/effect либо RESUME_INTENT и permit expiry. Receiver тратит свой slot до callback: ошибка проверки или потерянный ACK не восстанавливают его. PM_CANCEL/TIMEOUT/EMERGENCY_CLEANUP используют прежний coordinator. Recovery не создаёт receiver, не повторяет cancel и не запускает worker.

Ревью также обнаружило и исправило startup проверку inactive scope, profile environment reuse и потерю successful-create handles при ошибке закрытия inherited duplicates. Это статические исправления, их runtime свойства не доказаны.

## Проверки и подготовленная приёмка

Выполнены только AST syntax/readback/hash и scoped whitespace checks. Точная квитанция и SHA256 исходников: `.artifacts/serving-windows-20261109/source-manifest.json`. Импорты нового native кода, OS probes, pytest, fixture campaigns, native workers и модели не запускались. Полный lint/CI не выполнялся. Два исторических grants израсходованы; 114 PASS и 24 PASS относятся к старым пересекающимся наборам.

Подготовлены два regression cases retained-runner replay и четыре Windows no-model acceptance cases: exclusive owner/live predecessor, suspended query coverage/result, uncertain receiver authorization без повторного cancel, kill-on-close при сохранённом query process handle. Последние три создают по одному собственному synthetic Python root; они не являются Codex/model sessions. Тесты НЕ запускались.

Для первой новой fixture-only проверки требуется отдельный допуск: один проход через `scripts/run_tests.sh -j 1 --file-retries 0 --file-timeout 60`, cap 600 s, существующий test interpreter `<test-env>/Scripts/python.exe`, выделенные HOME/runtime/TEMP, scrubbed env, offline/no lazy installs. Файлы: `tests/plugins/test_symphony_pm_create_fencing.py`, `test_symphony_pm_lifecycle.py`, `test_symphony_pm_journal.py`, `test_symphony_pm_permit_source.py`, `test_symphony_pm_serving.py`. Native файл в этот допуск не входит. Новая установка зависимостей не разрешена.

Отдельный native smoke-кандидат: тот же runner, только `tests/plugins/test_symphony_pm_windows_native.py`, один проход без retries, cap 180 s, три synthetic roots суммарно, temp profiles/capture. Для него нужен самостоятельный native no-model допуск; сейчас не запускать. Убийство чужих процессов, microphone, model calls и сетевые запросы исключены scope этого кандидата.

## Конечная crash matrix и открытые риски

| Рубеж | Требуемый результат | Подготовка |
|---|---|---|
| Повтор retained runner после uncertain create/bind | Один durable create slot, прежний attempt, HOLD | Regression cases написаны, NOT_RUN |
| Два OS owners / живой predecessor | Второму отказ, lease не вытесняет первого | Native case написан, NOT_RUN |
| Suspended root / query observer / resume | Exact root+Job coverage до resume, query-only readback | Native case написан, NOT_RUN |
| Потерянная authorization/ACK | Slot spent, повторного effect нет | Fixture и native case написаны, NOT_RUN |
| Close последнего управляющего Job handle | Query process handle не удерживает Job; kernel stop отдельно от ACK | Native case написан, NOT_RUN |
| Controller takeover racing native entry | Stale epoch не проходит барьер; ≤1 native effect | Нужен native barrier harness после первого checkpoint |
| Observer loss до resume и после spent cancel | Admission закрыт, UNKNOWN; replacement не даёт sender | Нужен native barrier harness |
| Serving death между CreateProcess и Job assignment | Suspended orphan возможен; ни retry, ни ложного stopped/release | Незакрытый crash cut, обязательный HOLD; creation-time Job assignment ещё не реализован |
| Serving death после binding / effect до ACK / reboot | Сохранён intent; только query recovery, boot mismatch HOLD | Нужен crash harness; native smoke выше не закрывает этот ряд |

Матрица конечная, но crash harness последних четырёх строк ещё не реализован. Нельзя представить четыре native cases как полную crash qualification. Boot query ABI, Windows ACL фактического profile directory, race loss/readback, ограничение дискового overshoot между 200 ms проверками, native shutdown и реальная цепочка Codex launcher требуют Windows приёмки. Production approval source и квалифицированные host driver/validator/environment ещё не установлены. Новый CODE — кандидат с незакрытыми критериями, не завершённый serving transport.

## Голосовая цепочка

По read-only координации и Git object readback подтверждены Bimo handoff `8fbe6a162a1bc7cfd80957b1e8f1e99c2431ceaa`, candidate CODE `6812404aef26e9e42ce9f6024d7fb5cf15f60804`. Bimo владеет ASR/retrieval/GPU. Его код здесь не менялся. Интегратор сообщил: exact FINAL uid не проверяется; результат cancel HTTP может быть ошибкой при локальном CANCELLED; empty FINAL ещё несовместим с installed command provider. Это отдельные voice blockers, не результаты нового live теста.

План общего пути сохраняется: global hotkey → существующий voice ingress → FINAL preview → Hermes reply → exact host approval → существующий lifecycle/CodexDriver. Partial ASR не выдаёт grant. При потере Bimo остаются local STOP и текст. Global hotkey NOT_IMPLEMENTED; живой RU reply NOT_RUN. Установленный S3 по readback интегратора остаётся `c419cf7c864ac0ee511dbd9f7091a8bba0b567ee`; новой установки не было. Live mic/transfer/model/provider/retention и оплачиваемые ресурсы требуют отдельных точных решений.

## Статус

CODE подготовлен локально, не committed/pushed/installed. Native/global cancel и release HOLD; external stop, billing, power-loss и настоящий Codex stop UNKNOWN. До нового допуска на проверки кандидат не активировать. Сохранять журнал/identities/spent effects/reservations; не откатывать DB snapshot поверх uncertain intent. Чужие процессы и worktree не менять.
