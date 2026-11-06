# Handoff: единый durable cancel и проверка evidence

**CODE_REVIEW PASS; операционная приёмка cancel HOLD.** Исправленная локальная дельта прошла независимое read-only ревью GPT-6.1 Sol High с одним consolidated corrective pass. Исторический успешный live smoke не повторялся, новых Codex worker attempts — 0. Одна согласованная review-сессия, два прохода (Hygiene/финальный code review), завершена.

Точные source refs:

- Принятые опубликованные live artifacts: **ebcc60d2644df20bf2d8ca70735b29d8da3ee78e**.
- Исходный локальный corrective checkpoint: **6da26005e26c3255c3baf6a3f2c553c973b97aff**.
- Проверенная итоговая реализация: **b766aecb8524a5c0b78197cda7a144473af4fd07**, parent 6da26005. Ветка `codex/hermes-symphony-cancel-correction-20261106`, локальная. Новый push и merge не выполнялись.
- Каноническая corrective CR revision: `97d0f1cbd4a5906ff305fe0a8f2664ff0aea4124884f4e34cff2c2e89d5911cd`, Hygiene PASS/READY. Старый CR не переписан.

`cancel-correction-code-review.json` содержит итоговый независимый verdict, закрытые два lifecycle blockers и SHA256 точных Git blobs. `candidate-manifest.json` в b766aecb перечисляет проверенные staged Git blob hashes; не использовать raw working-copy EOL как идентификатор исходника. Первые три source-state claims исходного execution pack относятся к снимкам ДО исправления и STALE для нового runtime-кода; их нельзя наследовать как текущую приёмку. Новую работу строить из canonical CR + b766aecb + этого delta, а не из старой session/handoff narrative.

Что исправлено: PM cancel, TIMEOUT и явный EMERGENCY_CLEANUP зарегистрированного worker используют один durable effect key в прежней SQLite owner DB; причины сохраняются отдельно. Intent коммитится до OS dispatch. Receiver требует permit и тратит свою send slot до native entry, включая exception/lost reply. UNKNOWN сверяется read-only, без второго native dispatch. HOLD и POSSIBLY_CONSUMED_HELD не снимаются по локальному stop proof. Тайм-аут 180 секунд и drain 15 секунд сохранены даже при ошибке coordinator. Handle release в finally и общий cleanup проходят все attempts/owner/driver несмотря на диагностические ошибки. Ошибка handle release остаётся UNKNOWN, а kernel kill-on-close не называется acknowledged cancel.

Старый pre-admission rollback ещё suspended процесса сохраняет kill + wait=10; он до PM native binding и runtime watchdog, не является вторым зарегистрированным cancel path и не доказывает атомарность CreateProcess/storage.

Уточнение принятого smoke: открывалась новая SQLite connection при том же живом **LivePort**, controller и OS handles. Это НЕ controller restart. `cancel-recovery-correction.md` фиксирует ограничение отдельно; исходные immutable proof files и manifests сохранены.

Тесты: **29 cancel PASS + 26 evidence PASS = 55 актуальных cases** по целевым прогонам. После независимых cleanup findings заново выполнен затронутый cancel module: 29/29. Evidence module не менялся и сохраняет 25 PASS из основного прогона плюс 1 исправленный fixture case. File retries=0. Baseline опубликованного receiver подтверждён красным тестом без реального native API. Целевой Ruff и CC≤20 — PASS. Новый verifier проверяет 105 publication hashes, 46 evidence hashes, непустые proof sets и request/job/attempt/PID/start/session/ledger связи; проходит для immutable Git packet и исходного published export, также под `python -O`. Это consistency check записанного evidence, не повтор live событий.

Команды и полные результаты в `.artifacts/cancel-correction-20261106/REPORT.md`, `run-targeted.ps1`, `baseline-red.txt`, `targeted.txt`, `evidence-fix.txt`, `runtime-fix.txt`, `cleanup-fix.txt`.

Подготовлен **controller_restart_probe.py**, plan в `.artifacts/cancel-correction-20261106/restart-sandbox/plan.json`: статус **NOT_RUN**. Будущий probe — новый процесс C1, synthetic Python worker+child, native cancel, crash после эффекта до ack, новый процесс C2/new epoch, прежний journal и receiver=None. Query-only observer держит независимые handles. Parent проверяет retained C1 PID/start ДО admission worker и boundedly reaps собственный C1 при ошибках. Используется установленный base Python с текущими dependency paths; новых сред/ключей нет. Codex-сессий в этом probe — 0; Codex-вариант автоматически не разрешён.

Остаются UNKNOWN/NOT_RUN: новый native timeout/cancel path на Windows, реальный C1→C2 и OS failure-cleanup после исправления, последствия kernel close при UNKNOWN, process-creation/storage/power-loss qualification, serving-process admission/fencing/observer integration, external effects и billing settlement. Полный repository aggregate не установлен; прежний 144/144 относится к неизменённому public adapter/plugin/offline reference owner. Приватный kit/S2 не изменялся и не публиковался; чужие worktree, tracked файлы и shared index сохранены.

Следующий достаточный шаг к работающему транспорту — отдельная команда владельца на минимальный **нулевой-model** C1→C2 sandbox probe в текущем Windows. После реального результата квалифицировать восстановление владельца и observer в serving process. HTTP-сервер/новая очередь/новые ключи для этого не нужны. Новый настоящий Codex worker либо транспортный pilot требуют отдельного согласования модели, среды и exact launch scope. До подтверждения остановки и последствий cancel остаётся HOLD, резервация удерживается.
