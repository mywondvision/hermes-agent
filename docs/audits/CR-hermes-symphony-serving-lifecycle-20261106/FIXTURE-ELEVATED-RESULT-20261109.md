# Fixture проверка нового CODE: 26 PASS, native HOLD

## Допуск и результат

Владелец ответил `да` на один повтор той же fixture-only команды с повышенным доступом, cap 600 s, без retries/native/model/install. Штатное повышение доступа разрешило запуск. Предыдущая попытка `fixture-campaign-1` завершилась до тестов из-за MSYS CreateFileMapping/Win32 error 5; её квитанции не изменялись.

Повышенный проход: **5 файлов, 26 PASS, 0 FAIL**. Полная команда заняла **97.422 s**, test runner **93.5 s**. Внутренних повторов нет (`--file-retries 0`). Это второй общий вызов обёртки, но первый и единственный вызов по новому допуску; поле `attempt: 1` сырой квитанции относится к этому новому grant.

| Файл | Результат | Время subprocess |
|---|---|---|
| test_symphony_pm_create_fencing.py | 2 PASS | 22.12 s |
| test_symphony_pm_journal.py | 6 PASS | 16.00 s |
| test_symphony_pm_lifecycle.py | 7 PASS | 16.58 s |
| test_symphony_pm_permit_source.py | 5 PASS | 14.69 s |
| test_symphony_pm_serving.py | 6 PASS | 23.19 s |

Новые regression cases проверили, что сохранённый AttemptRunner после uncertain create либо cut до bind не получает второй create slot. Остальные случаи проверили journal, lifecycle, permit source и discovery/bootstrap на fixture providers. Реальные Windows ownership/receiver/observer/CodexDriver integration этим проходом не квалифицированы. Исторические 114 PASS и 24 PASS с новым результатом не складываются: наборы пересекаются.

## Воспроизводимость и readback

HEAD и ветка до и после: `16ea4b7abbb2541c61c7059f3bde6b3c784d261a`, `codex/hermes-symphony-cancel-correction-20261106`. Все **23** закреплённых source/report hashes совпали после выполнения; source changes: `[]`. Чужие tracked modifications остались прежними. Индекс пуст; commit/push/install не выполнялись.

Точная команда:

```text
python -X utf8 -I -B .artifacts/serving-windows-20261109/fixture-elevated-attempt-2/run-fixture-campaign.py
```

Она выполнялась с `require_escalated` и вызывала:

```text
C:/Program Files/Git/bin/bash.exe scripts/run_tests.sh -j 1 --file-retries 0 --file-timeout 60 tests/plugins/test_symphony_pm_create_fencing.py tests/plugins/test_symphony_pm_lifecycle.py tests/plugins/test_symphony_pm_journal.py tests/plugins/test_symphony_pm_permit_source.py tests/plugins/test_symphony_pm_serving.py
```

Использован существующий test interpreter `<test-env>/Scripts/python.exe`; выделенные HOME/runtime/TEMP, scrubbed env/offline/no lazy installs. Новый interpreter или зависимости не устанавливались. Subprocesses принадлежат штатному test runner, не native task workers.

Квитанции: `.artifacts/serving-windows-20261109/fixture-elevated-attempt-2/` — plan, attempt, runner, source manifest, log, raw result и verification. Plan `CONSUMED_FINISHED_PASS`, remaining=0.

- `campaign.log` SHA256: `9903f0eacef97bff0ee825bd6783ecdb103baffb3f4f4a03e0785c6f9f537507`.
- `verification.json` SHA256: `d8ec9ead44bf4473dc977111bcdc1de0177b5064063296248ef16c5b5840b156`.
- Исходный candidate manifest SHA256 остаётся `d0b78da071061c10920466a5d9ac527111b30092e69a9ae125f7578f0a299534`; его исторические NOT_RUN не переписаны. Текущий fixture результат задаёт эта более поздняя квитанция.

## Что остаётся

Native cancel/release **HOLD**. Native workers, microphone, model calls, live runtime и установка не запускались. Full lint/CI не выполнялся. Fixture grant израсходован и не разрешает native smoke.

Следующая CODE-подготовка: завершить native crash harness для takeover race, observer loss и serving death/recovery. Четыре ранее подготовленных native acceptance cases не закрывают эту матрицу полностью. В частности create→Job assignment cut может оставить suspended orphan; production qualification остаётся незакрытой. После подготовки — отдельный точный native no-model допуск, без повторения исторического model smoke. Approval source/qualified driver/environment bindings, live voice и external/billing stop остаются отдельными вопросами, как в WINDOWS-CANDIDATE-20261109.md.
