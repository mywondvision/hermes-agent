# Native Windows компоненты: 4 PASS; crash candidate подготовлен, cancel HOLD

## Новый допуск и результат

Владелец ответил `допускаю` после указания отдельного native допуска. Исполнитель зафиксировал узкую область ранее подготовленного native smoke: один файл, четыре Windows acceptance cases, максимум три test-owned synthetic Python roots, один проход без retries, cap 180 s. Модели, микрофон, внешняя речь, расходы, установка, push и production не входили. Штатное повышение доступа разрешило выполнение.

Результат: **4 PASS, 0 FAIL**, команда **19.328 s**, runner **16.3 s**. Выполнены реальные Windows API на собственных synthetic-процессах:

1. Exclusive OS guard не допускает второго owner; живой exact predecessor отказывает.
2. Suspended root имеет query coverage; после resume получен synthetic result и query-only stopped readback.
3. Uncertain authorization расходует receiver cancel slot; повторный sender не выдаётся.
4. Закрытие управляющего Job приводит к kernel stop даже при сохранённом process query handle.

В этой кампании **не вызывался успешный TerminateJobObject через полный production coordinator**, не выполнялись serving crash/recovery, смена controller, CodexDriver или модель. Тестовый authorize callback не является production grant. Это native component evidence, не native/global cancel acceptance.

Точная команда:

```text
python -X utf8 -I -B .artifacts/serving-windows-20261109/native-campaign-1/run-native-campaign.py
```

Она вызывает `scripts/run_tests.sh -j 1 --file-retries 0 --file-timeout 60 tests/plugins/test_symphony_pm_windows_native.py` через Git Bash. Existing test interpreter, отдельные HOME/runtime/TEMP, scrubbed env/offline/no lazy installs. Повторов нет. Plan `CONSUMED_FINISHED_PASS`, remaining=0. Предыдущие grants не переиспользовались.

Все 23 закреплённых hashes совпали сразу после кампании; HEAD `16ea4b7abbb2541c61c7059f3bde6b3c784d261a`, ветка `codex/hermes-symphony-cancel-correction-20261106`. Log SHA256 `b4d41606ab08a0b70594f9af606deb69db63f6b25189fde675e402ba181e82e1`. Raw result и verification лежат в `.artifacts/serving-windows-20261109/native-campaign-1/`. Прежние квитанции не переписаны.

## После кампании: E0 correction, ещё не runtime-qualified

Независимый read-only reviewer обнаружил несоответствие `windows_ownership.py` исходному CR, пункт 5, строка 69: «отсутствие доступа/identity mismatch означает HOLD». Код принимал несовпадение creation time за допустимое доказательство смерти predecessor. Узкая коррекция теперь явно отказывает `PREDECESSOR_IDENTITY_UNKNOWN`. Добавлен отдельный native case без создания worker.

**Четыре PASS относятся к исходникам ДО этой коррекции.** Старый source manifest сохранён; новый candidate manifest отмечает изменённый ownership source и два новых test files. Изменённая ветка кода проверена только AST/readback. Её PASS не заявляется.

## Crash harness: подготовлено, НЕ запущено

Новые test-only файлы:

- `tests/plugins/symphony_native_crash_support.py`: actual providers, exact synthetic command, bounded worker, fsynced identity receipt и явный file barrier перед `os._exit(71)`. Отдельный runtime/dispatcher/daemon не создаётся.
- `tests/plugins/test_symphony_pm_windows_crash.py`: семь cases, максимум девять synthetic-процессов — три roots в текущем test host плюс три пары serving helper/root; identity mismatch не создаёт процесс.

| Сценарий | Что требуется подтвердить |
|---|---|
| Predecessor creation mismatch | HOLD, без native effect |
| Controller takeover при native entry | Реальная contention на том же RLock, один effect до rotation; поздний captured callback после rotation отвергнут |
| Observer loss после intent до effect | UNKNOWN, ноль terminate; новая observer generation не выдаёт sender |
| Terminate прошёл, ACK потерян | Один terminate, UNKNOWN effect, прежний dispatch epoch после observer replacement |
| Serving death до Job assignment | CREATE_INTENT сохраняется, root ещё жив: известный orphan; recovery не создаёт worker; отдельная test containment cleanup, не cancel ACK |
| Serving death после binding до resume | RESUME_INTENT сохраняется; kernel stop, missing Job даёт recovery HOLD |
| Serving death после cancel effect до ACK | DISPATCH_INTENT сохраняется; query stop, recovery не создаёт receiver или send slot |

Serving helper создаётся suspended и включается в отдельный parent-owned test containment Job до resume. Этот Job остаётся OPEN во время проверки смерти worker для before_resume/after_effect и не объясняет их stopped proof. Для before_assign сначала проверяются живой orphan и отсутствие нового create при recovery, затем parent Job закрывается как test cleanup. Таким образом assertion/error не убивает единственного владельца уборки и не оставляет suspended orphan из-за самого harness. На cleanup проверяются exact retained process handles; чужие PID не сканируются и не останавливаются. Дополнительный test Job не переносится в production дизайн и не закрывает production create→assign gap.

Reviewer выполнил только чтение, без записи или дополнительных запусков. Замечания по безопасной уборке, mismatch и deterministic contention внесены. Самопроверка: AST и whitespace для 25 source/report files; ни импорт, ни runtime нового harness не выполнялись.

Новый manifest: `.artifacts/serving-windows-20261109/native-crash-candidate/syntax-and-source-manifest.json`, SHA256 `ebcbaf0421a643c67b6a78b4090cac6428563c2910f85f8ca22375fd40625d8a`. Prepared plan содержит `owner_approved: false`, `PREPARED_NOT_APPROVED_NOT_RUN`. Runner отказывает без нового допуска.

Конкретный будущий допуск: **один проход семи cases в одном новом файле**, максимум девять synthetic-процессов, cap **600 s**, file timeout **300 s**, retries **0**, existing interpreter, temp profile/runtime/TEMP, без моделей/микрофона/сети/расходов/установки. Команда: `python -X utf8 -I -B .artifacts/serving-windows-20261109/native-crash-candidate/run-crash-campaign.py`. Старые native четыре cases повторять не требуется. Это предложение, не автоматическое продолжение израсходованного smoke grant.

## Текущий итог

Fixture evidence: 26 PASS (отдельный предыдущий проход). Native component evidence: 4 PASS. Новый crash harness и identity correction: E0, runtime NOT_RUN. Наборы и уровни доказательств не складываются в общую квалификацию.

Native/global cancel и release **HOLD**. Production create→assign gap остаётся. Driver/issuer/environment bindings не установлены, новый CODE не committed/pushed/installed. Реальная голосовая цепочка, модельный stop, remote inference и billing — отдельные незакрытые вопросы. Голосовой интегратор уведомляется отдельно; единственный writer этого worktree сохраняется.
