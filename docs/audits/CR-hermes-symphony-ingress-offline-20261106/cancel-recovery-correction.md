# Уточнение принятого live smoke

Исходное immutable свидетельство: ebcc60d2644df20bf2d8ca70735b29d8da3ee78e, `.artifacts/live-smoke-20261106/approved-retry-1/`.

В smoke выполнялся reopen SQLite connection. Существующий объект `LivePort` оставался тем же: заменялось только его поле `owner`, перепривязывался handler через `wire()`. Словарь attempts, controller process, receiver objects, Windows Job и process handles сохранялись живыми. Формулировка «port reopened» в старом отчёте не означает создание нового LivePort или перезапуск контроллера.

Свидетельства успешного Codex JSON, однократного локального native cancel в проверенном PM path, lost-response/redelivery и OS tree stop сохраняются. Они не покрывают гонку timeout/PM, прямой bypass receiver, полный controller restart, power-loss/storage qualification, external effects settlement или billing settlement. Общий cancel остаётся HOLD, reservation — POSSIBLY_CONSUMED_HELD.

Новая корректирующая дельта закрывает явные PM/timeout/cleanup paths на уровне кода и офлайн-тестов. Новые live worker attempts не выполнялись. Реальный C1→C2 process restart только подготовлен в `controller_restart_probe.py` и `restart-sandbox/plan.json`; его статус NOT_RUN. Prepared probe использует синтетический Python worker, не Codex. Любой Codex-вариант требует нового согласования модели/среды и exact launch scope.
