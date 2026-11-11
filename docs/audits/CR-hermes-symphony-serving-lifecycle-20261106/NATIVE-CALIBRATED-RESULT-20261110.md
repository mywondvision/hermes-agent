# Калиброванный synthetic native lifecycle: 5 PASS, global cancel HOLD

## Проверенный результат

Владелец ответил `ДА` на один проход пяти затронутых cases, бюджет 14 физических test-owned процессов с учётом console hosts, cap 600 s, retries 0, без моделей/микрофона/установки/расходов. До запуска совпали все 26 hashes reviewed source manifest. Штатное повышение доступа разрешило выполнение.

**5 PASS, 0 FAIL, 2 deselected**. Полная команда **12.875 s**, runner **10.6 s**. Повторов нет. Сохранённые rosters дают **14 уникальных `(PID, creation)`**, для всех 14 есть terminal `signaled=true`, ошибок cleanup в proofs нет. Ни один member не исключён из coverage по имени. Бюджет подтверждён данными, а не только оценкой числа launches.

Команда:

```text
python -X utf8 -I -B .artifacts/serving-windows-20261109/native-crash-calibrated/run-calibrated-campaign.py
```

Canonical runner: `scripts/run_tests.sh -j 1 --file-retries 0 --file-timeout 300 tests/plugins/test_symphony_pm_windows_crash.py -v -k "lost_ack or serving_crash or takeover"`. Existing test interpreter, temp HOME/runtime/TEMP, scrubbed env/offline/no lazy installs. Plan `CONSUMED_FINISHED_PASS`, remaining=0. Предыдущие grants и результаты не переписаны.

## Что подтверждено

| Case | Результат и evidence |
|---|---|
| Takeover при native entry | Реальная contention на прежнем RLock; один native effect до rotation, epoch увеличился один раз; поздний callback старого token отвергнут, второго effect нет |
| Потерянный ACK | Terminate выполнен один раз, exception оставляет UNKNOWN; replacement observer получает новую generation и не выдаёт повторный sender, исходный effect не меняется |
| Serving death до Job assignment | Непривязанный CREATE_INTENT остаётся spent, recovery не создаёт worker. Suspended orphan воспроизведён; его остановка — отдельная parent test containment cleanup, **не исправление production gap и не cancel ACK** |
| Serving death после binding до resume | Сохраняется RESUME_INTENT; worker root signaled до закрытия parent Job; missing Job при recovery даёт HOLD, launch/effect rows не меняются |
| Serving death после effect до ACK | Worker root и второй измеренный member signaled при ещё открытом parent containment Job и до crash release; DISPATCH_INTENT переживает смерть helper, recovery не создаёт receiver/send slot |

Worker rosters имеют `Total=2`; parent rosters `Total=3/3/4`. Kernel active limits 2 для worker Job и 4 для parent containment Job. Бюджет пяти cases: 2+2+3+3+4=14, повторно встречающиеся identities в разных proof files дедуплицированы по **PID+creation**, не по одному PID.

Pre-cut proof `worker-before-cut-after_effect.json`: два worker-member handles `signaled=true`, `parent_containment_open=true`. Поздний parent cleanup не объясняет этот stop. Parent terminal proofs отдельно подтверждают последующую quiescence всех 3/3/4 известных identities; worker terminal proofs — 2/2 в in-process cases. Для before_assign явно сохранён `orphan_cleanup=true`.

Recovery assertions внутри прошедших cases сравнивают полный набор launch/effect rows до и после recovery и проверяют DISPATCH_INTENT. Raw SQLite rows после pytest scratch cleanup не сохранены; этот слой evidence является **проверенными test assertions**, а не независимо перечитанным архивом БД. JSON identities, counters, terminal proof и hashes сохранены отдельно. Reboot/power-loss этим не проверены.

## SHA и квитанции

- HEAD до и после: `16ea4b7abbb2541c61c7059f3bde6b3c784d261a`.
- Ветка: `codex/hermes-symphony-cancel-correction-20261106`.
- Source manifest SHA256: `b3291f80f8a0d6f1de5831d295200e6c7518400b3b797c2d12e79f71ccdd70ba`; все **26** hashes совпали после кампании, source changes `[]`.
- Log SHA256: `f925bd51699ca14c1b1d829e707afad0ac32645d9d6a6ddd92e7b9b1982afac9`.
- Verification SHA256: `af725d00713c916d4c2bc8dca641d56223d1a40d4c7301c93ad27b0055182fd4`.
- Proof index SHA256: `7c4a3a6699b8f1c246b04ed9bd968a1205561a2b76a1639cb1ffc7f78efe20e9`.

Все квитанции находятся в `.artifacts/serving-windows-20261109/native-crash-calibrated/`: plan, attempt, runner, source manifest, log, raw result, verification, proof-index и 18 JSON proofs. Исходники не менялись после reviewed manifest. Индекс Git пуст; прежние чужие tracked/untracked изменения сохранены. Commit/push/install не выполнялись.

Независимый reviewer прочитал log и все JSON proofs без запуска tests/probes и подтвердил 5 PASS, 14 unique identities, terminal coverage всех 14, отсутствие cleanup errors и отдельный pre-parent-cleanup worker proof. Consumed/remaining=0 затем прочитаны parent из обновлённого plan. Это независимый read-only evidence review, не дополнительная native campaign и не новый Context Hygiene PASS.

## Граница результата и следующий этап

Разрешённый synthetic native этап пяти cases завершён успешно. Предыдущие fixture 26 PASS, native component 4 PASS, diagnostic 1 PASS и исторические 114/24 — отдельные, частично пересекающиеся проверки разных source revisions; их нельзя суммировать или объявить полным CI/runtime acceptance.

**Native/global cancel, release и production readiness остаются HOLD.** Реальный CodexDriver/model stop, внешний inference и billing не проверялись. Production approval source и qualified driver/environment bindings не установлены. Production create→Job assignment gap остаётся: текущая реализация держит orphan/uncertain launch на HOLD и запрещает relaunch, но не гарантирует самостоятельную остановку orphan при смерти owner до assignment. Parent Job в тесте — только страховка стенда.

Не выполнялись: installation в S3, model/native Codex session, микрофон, передача живого голоса, paid ресурсы, полный lint/CI, reboot/power-loss, push или merge. Новая CODE остаётся локальным review candidate. Для следующего operational шага сначала нужен concrete host/driver/issuer execution pack и отдельный exact model/environment/attempt/installation permit; текущий израсходованный допуск этого не разрешает. Повторить пройденную кампанию ради уже доказанных свойств не требуется.

Голосовой интегратор получает этот результат отдельно. Готовность global hotkey/RU voice и Bimo ASR не повышается по результатам lifecycle tests; writer ownership этого worktree сохраняется за текущей сессией до явной передачи.
