# Допуск на исправление машинного отчёта

2026-11-09. Владелец ответил `разрешаю` на вопрос об одной коррекции только JSON-отчёта тем же reviewer, без повторного аудита и изменения CR. Подтверждение ограничено завершением ранее полученного отчёта `amended-review.json` для неизменных CR revision `d408f7b5b619e13ab13fd113b428b7e5633399719d0b7732df7fa66a8d2d691d` и Context Pack.

Разрешён один ответ прежнего `root.serving_binding_hygiene` с исправлением отчётных полей, выявленных `amended-validation.json`. Preparer сохраняет исходный ответ, применяет только явный patch reviewer к отдельному файлу и запускает штатную metadata validation. Самостоятельное изменение verdict, authority, требований, неизвестного, CR или Context Pack этим не разрешается.

Не разрешены новый аудит, иной reviewer, рекурсия/новые модели, tests/runtime/native/model запуск, микрофон, передача речи, paid resources, production, установка, ключи/доступы, push и merge. Parent proof budget и прежние permissions не обнуляются. После этой коррекции дополнительных reviewer-проходов не остаётся.
