# S3/S4 voice: состав публикации

Публикуются только stage manifest, предыдущий voice precheck, read-only inventory scripts и выбранные metadata/evidence этой голосовой линии. Родитель — опубликованный live-smoke commit `ebcc60d2644df20bf2d8ca70735b29d8da3ee78e`; его исходники и доказательства не изменяются.

Статусы остаются прежними: manifest DRAFT/HYGIENE_PENDING, runtime не подтверждён, global voice не реализован, новый audio/native smoke NOT RUN, общий cancel HOLD. Публикация не является INSTALL/code/audio/session permit, приёмкой S4 или merge.

Не включены disposable test homes/profiles/runtime/cache, raw MSYS stack dump, speech/model payloads, credentials, оригиналы CR из Downloads, private kit и чужие untracked файлы. Для MSYS сбоя опубликована отдельная redacted summary; исходный raw log остаётся локально. Пустые stdout/stderr не публикуются. Все остальные выбранные файлы публикуются без подмены содержания.

Машинные пути в отчётах — исторические evidence locators, не гарантированные пути на машине читателя. Для проверки dependency inventory нужен `packaging` и соответствующий Windows/Python target; это offline оценка, не install proof. Голосовые тесты запускались только через canonical runner и не начались из-за MSYS `Win32 error 5`. Read-only verifier старого smoke подтвердил согласованность сохранённых evidence, а не повторил live события.

Коммит создаётся в отдельной ветке с отдельным индексом без checkout. Рабочий HEAD и общий индекс исходного checkout не меняются; файлы параллельного исполнителя сохраняются.
