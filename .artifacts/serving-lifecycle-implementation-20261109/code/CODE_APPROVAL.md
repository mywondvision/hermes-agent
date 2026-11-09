# CODE-поправка: основание исполнения

2026-11-09. Владелец ответил `ок` после валидного Hygiene PASS/READY ревизии `d408f7b5b619e13ab13fd113b428b7e5633399719d0b7732df7fa66a8d2d691d` и запроса `ок на CODE-поправку`. Это разрешение реализовать generic host binding в той же линии CR. Исходный CODE approval сохраняется. Новых writer/runtime/queue/dispatcher не создаётся.

Допуск не включает выполнение новой no-model E1/E2 campaign, transport pilot, native/model sessions, микрофон, передачу речи, paid resources, production, новые credentials/доступы, установку, commit/push/merge. Неизвестный production approval source не подменяется тестовым issuer или разрешением на разработку.

Последующее сообщение владельца `сбросил` не трактовалось как отмена задачи или разрешение на восстановление файлов. Владелец уточнил: `лимиты`. Readback подтвердил HEAD `8169f1b66a3a7c6844e3347c89f5cfa82edd4483` и сохранность новых файлов. Сброс worktree по проверенным путям не наблюдался. Чужие изменения сохраняются.

Review/JSON correction budgets исчерпаны и не сбрасываются. Предыдущие independent reports неизменны. Разрешены authoring и статическая проверка исходников; code under test пока не исполняется. AST parse/compile ниже не является unit/runtime evidence.
