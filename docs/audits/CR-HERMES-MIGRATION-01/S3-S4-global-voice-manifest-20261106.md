# CR-HERMES-MIGRATION-01 · S3 manifest / S4 global voice

context_hygiene: HYGIENE_PENDING

Статус: **DRAFT, INSTALL / CODE / AUDIO / NATIVE EXECUTION NOT AUTHORIZED**. Это stage manifest существующего r0.2 + r0.3, не новая очередь, не новая кампания и не разрешение обнулить бюджет. Исходные CR не меняются. Последнее поручение владельца — продолжать manifest, не повторять широкий поиск и сохранить OpenWhispr 1.10.1.

## Целевой интерфейс

Глобальная клавиша из другого приложения → один захват → один STT → нужная сессия Hermes → ответ → при наличии разрешённого намерения и scope permit тот же Symphony/Codex → результат пользователю. Ручная вставка и обязательный Enter/confirm каждой обычной команды не являются конечной приёмкой. Preview остаётся доступным для исправления; уточнение требуется при неоднозначности, согласование эффекта — по характеру действия и действующему разрешению. Ни voice, ни LLM не создают authority.

Кандидат минимальной доработки: расширить существующую Electron Quick Entry/globalShortcut relay и подключить **встроенный** recorder/voice engine главного renderer. Quick Entry не получает второй gateway, микрофон, ASR, scheduler или новый model dispatcher. Не вводить direct voice-to-shell. Только после отдельного code grant. Возможность globalShortcut работать без focus подтверждена [официальной документацией Electron](https://www.electronjs.org/docs/latest/api/global-shortcut/); фактическая регистрация и работа на этом ПК ещё не проверены.

Перед capture фиксировать connection/profile + durable/runtime session identity. Не использовать поздний `current` после await. Смена активного приложения/окна/профиля не переадресует utterance. Исчезнувшая/другая сессия → HOLD, без автосоздания. Повтор во время capture завершает запись; повтор во время transcription/submission не создаёт второй запрос. Отдельная глобальная отмена запрещает позднюю отправку. Конфликт клавиш → видимая ошибка; не забирать OpenWhispr shortcut. Предложение: Ctrl+Alt+F9 record/finish, Ctrl+Alt+F10 local voice cancel; назначение и debounce/repeat требуют проверки до audio.

Возможные edge cases: minimized main renderer, background throttling, окно Quick Entry теряет focus, результат приходит после смены профиля, reconnect/timeout при submit. Они входят в одну квалификацию выбранного frontend. Никакой clipboard paste или synthetic typing в активное окно.

## Проверенная база и владельцы

- Hermes source: `D:/AI/worktrees/hermes-pm-ingress-20261031`, detached `c419cf7c864ac0ee511dbd9f7091a8bba0b567ee`; tracked diff пуст на проверке. Source release placeholder `0.0.0` не является установленной версией.
- Второй Git worktree зарегистрирован: `D:/Codex/codex-home/worktrees/cf6e/hermes-pm-ingress-20261031`, `369a012cc9b5caece4b1a6e37fdecb1ca04530b3`, ветка `codex/symphony-pm-offline-20261106`. Это регистрация, не подтверждение текущего writer/доступного runtime.
- Исходный checkout и PM/ingress файлы считаются занятыми другим исполнителем. Этот S4 writer пишет только свои документы/evidence; будущая сборка/изменение frontend — в отдельной source copy ниже. Общий adapter/owner/ledger/timeout код не входит в allowlist.
- Попытка согласования с существующим live-smoke thread `01a1b1b0-193d-7871-a816-abf793ee4ac2` отклонена `already has an active writer`; повторов, открытия/изменения его session/config или запуска задач не было. **Согласование не завершено.**
- Runtime не найден в проверенных PATH, именованных процессах, StartApps, Appx, uninstall registrations, стандартных Windows Hermes data roots. Именованного HERMES_HOME в пользовательском Environment registry не обнаружено. Portable runtime вне этого охвата UNKNOWN; полный диск не сканировался повторно.
- Зарегистрирован OpenWhispr 1.10.1. Приложение, модели, настройки и hotkeys сохраняются до равноценной проверки. Если потребуется fallback, он получает единоличный capture+ASR и адресный API relay без повторного STT и без вставки в случайное окно; пригодность такого API ещё не установлена.

r0.2: `C:/Users/Екатерина/Downloads/CR-HERMES-MIGRATION-01-r0.2-consolidated.md`, 272770 bytes, SHA256 `c8c5a2aee4ca1f2606ab0e104d205bf5adfe41ba695135ed7a55da85da7a067e`. r0.3: соседний `CR-HERMES-MIGRATION-01-r0.3-candidate-delta.md`, 13137 bytes, SHA256 `4be28257aae36555e9ae146838d3c15713235223b0b666a201e1a6da89c755fd`. Изучены §6, S3/S4, включённые матрица/security/12 fixtures; r0.3 Hygiene PASS не является INSTALL/audio/native grant.

## Изолированные пути — предложение, ещё не созданы

Root `D:/AI/worktrees/hermes-pm-ingress-20261031/.artifacts/s4-global-voice-20261106/prepared` (`$S3`).

| Назначение | Путь под `$S3` |
|---|---|
| Новая source copy одного SHA | `home/hermes-agent` |
| Пользовательский home/profile | `home`; только dedicated default profile, без clone существующих homes |
| PM tools/runtime | `runtime` |
| Python environment | `python-env` |
| Electron userData | `desktop-user-data` |
| ASR model | `home/models/faster-whisper-base-ebe41f7` |
| HF/npm/PM caches | `cache/huggingface`, `cache/npm`, `cache/pm` |
| Log, installation receipt, hashes | `evidence` |
| Речь, только после retention/audio grant | `audio` |

Перед созданием убедиться, что prepared root отсутствует либо принадлежит точно этому manifest; существующее дерево не удалять/пересоздавать. Source copy — tracked tree указанного HEAD, без чужих untracked, auth, `.env`, state, logs и private kit. Не создавать новый Git worktree или копию общего ledger. Не менять глобальный PATH, services, autostart, Windows audio/network settings, npm cache или существующую тестовую среду.

## Версии, зависимости и загрузки

Фиксация всех source/lock hashes: `.artifacts/s4-global-voice-20261106/inventory.json`. Переход на другой SHA требует новой привязки зависимого evidence.

| Компонент | Закрепление / размер |
|---|---|
| Python | Существующий `D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe`, 3.14.9, Windows AMD64. Использовать как bootstrap, не устанавливать пакеты в него. Новый Python download не запрошен |
| Python deps | Frozen `uv.lock`, SHA256 `e6d08c634c3f9a252a896135a9c30324776fa343ce68432e88d22f2968f257e2`; только core + `audio-io`, `stt-whisper`, `web`, без `all`/wake/browser/канальных/TTS extras |
| Target wheel inventory | **90 wheels, 143370837 bytes (136.73 MiB)** выбранных compressed artifacts без учёта reuse. Offline marker/tag walk, не результат PM resolver. URL/SHA256/size каждого в `python-artifacts.json`; sdist-only/ambiguous candidates не обнаружены. PM manager/build support и runtime startup ещё требуют проверки |
| STT versions | faster-whisper 1.2.1, sounddevice 0.5.6, numpy 2.5.4; прочие exact versions/hashes в wheel inventory. Только CPU/int8, русский `ru`, file STT, no automatic cloud fallback |
| Node/npm | Lock `pm/lock.json`: отдельные Node **26.12.1**, npm **12.0.2**. Системный Node **20.15.0** не соответствует Desktop `>=26` и не обновляется. Есть запись PM cache о 26.12.1, но доступность/целостность binary здесь не подтверждена |
| Desktop | Root package-lock integrity; Electron **44.9.3**, TypeScript **6.0.3**, Vite **8.3.4**, Vitest **4.1.12**. Сборка только нужного Desktop workspace, без root install scripts и global npm install |
| STT model | **Systran/faster-whisper-base**, revision **ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66**, MIT, multi-language CTranslate2. [Pinned files](https://huggingface.co/Systran/faster-whisper-base/tree/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66) показывают около **148 MB** total; weight `model.bin` **145217532 bytes**, SHA256 `d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9`. Config/tokenizer/vocabulary обязательны; точные byte sizes/hash всех ancillary files ещё не зафиксированы. Не загружать только weights и не использовать `main` при install |
| Имеющаяся модель OpenWhispr | `ggml-medium.bin` 1533763059 bytes, сохраняется. Не совместима напрямую с выбранным CTranslate2 path; не конвертировать/заменять автоматически |
| Hermes answer model | **UNKNOWN / не выбран**. Не импортировать external auth/login, не запускать Ollama/llama server или cloud inference в S3. STT модель не выдаётся за модель ответа |
| Codex test task | Прежний smoke использовал gpt-6.1-sol High / Windows read-only. Старый native permit исчерпан; новое предложение ровно одной read-only задачи требует нового exact model/environment/session permit и принятого receiving path |

По закреплённому `pm/lock.json`: Node Windows x64 archive SHA256 `b3efd5c70cba3308ed46e7e699a9b8aac075bbc6b027fcb39c84c31acc25e2b4`; npm tgz SHA256 `5dbb86c71d07a1957f2e90734092dd6a58bdcd9ebc2d8d41ca1c6e6a21d364e1`. URLs из lock сохранены в `public-size-probes.json`. HEAD-only metadata запросы для Node/npm/Electron получили `WinError 10061`; payloads не скачивались, proxy/auth/network controls не менялись. **Их точные размеры и compressed size JS closure остаются UNKNOWN.** Не подменять их произвольной оценкой.

Новые runtime/model payload downloads в этой проверке: **0 bytes**; public documentation/metadata reads выполнялись. Около **291 MB** Python wheels+ASR model — лишь частичный размер, **не общий размер установки**. Предложенный ceiling CR ≤10 GiB/≤60 min не является текущим разрешением или измеренным объёмом. До INSTALL нужны полный download manifest либо явное решение владельца о capped preparation с остановкой на неизвестном/превышении; нельзя автоматически расширить лимит. Бинарный Electron download требует platform checksum из закреплённого release; npm integrity одного installer package недостаточно.

## Команды — план, НЕ выполнено

После INSTALL grant, source copy, полного artifact manifest и подтверждения инструментария:

```powershell
$S3 = 'D:/AI/worktrees/hermes-pm-ingress-20261031/.artifacts/s4-global-voice-20261106/prepared'
$env:HERMES_HOME = Join-Path $S3 'home'
$env:HERMES_RUNTIME_DIR = Join-Path $S3 'runtime'
$env:HERMES_DESKTOP_USER_DATA_DIR = Join-Path $S3 'desktop-user-data'
$env:UV_CACHE_DIR = Join-Path $S3 'cache/pm'
$env:npm_config_cache = Join-Path $S3 'cache/npm'
& 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe' -B -m pm.build_env --source "$S3/home/hermes-agent" --out "$S3/python-env" --python 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe' --extra audio-io --extra stt-whisper --extra web --offline
```

`--offline` — первый cache-only build; cache miss означает HOLD, не автоматический network retry. Для разрешённой загрузки те же pinned requirements/hashes, выделенный cache и конечный byte/time budget. `activate.ps1`, полный setup, `--all-extras`, raw pip/uv, `npm update` и переустановка существующих сред исключены.

После разрешённой подготовки exact Node/npm в task-owned runtime, cwd source copy:

```powershell
# $S4Node и $S4NpmCli берутся из проверенного PM receipt, не из системного PATH.
& $S4Node $S4NpmCli ci --ignore-scripts --workspace apps/desktop --workspace apps/shared --include-workspace-root
& $S4Node $S4NpmCli run build --workspace apps/desktop
# Кандидат запуска уже собранной копии, cwd $S3/home/hermes-agent:
& "$S3/home/hermes-agent/node_modules/.bin/electron.cmd" apps/desktop
```

Это **launch candidate, не принятая безопасная команда**. Перед запуском доказать, что Desktop использует именно новый home/interpreter/канонический gateway, не клонирует/обновляет runtime, не заходит в default home, не adopts external logins, не загружает hooks/plugins/channels, не делает automatic auth/model calls. Поддерживаемый bootstrap binding Python environment ↔ canonical gateway ещё UNKNOWN. `npm ci --ignore-scripts` сам не ставит Electron binary; требуемый pinned checksum-verified binary и scripts должны быть перечислены отдельно, никаких auto postinstall. Если это не обеспечено — HOLD без запуска приложения.

Новый smoke config сначала с STT disabled/no model calls; после audio grant — explicit local model path, `ru`, offline HF, lazy installs off, auth adoption off, wake/new-session/goal/kanban/cron/PM engineering dispatch и auxiliary fallbacks off. `client_direct: false` исключает credential egress. No-tools voice smoke отдельно от gated single-task phase. Настройки фиксируются до conversation, не меняют system/tool surface mid-session.

## S4: конечные проверки и отдельные допуски

1. Сначала synthetic global-hotkey/state/routing tests без микрофона/модели. Один event handler/recorder; repeat/double press, silence cancel, blur, session/profile switch, late transcript/submit, timeout/reconnect → no duplicate dispatch. Callback generation не считается remote cancel proof. Ошибка STT сохраняет согласованный task-owned recording/evidence; автоматическое удаление записи не принимается как retention policy.
2. После accepted S3 и отдельного mic/audio/session permit: другое приложение → global key → русская речь → встроенный STT → один короткий ответ без tools/effects. `125, не 152`, запреты, self-correction, silence/noise, cancel, repeat press, focus switch. Каждый case имеет timestamps и exact target. Не требовать подтверждения обычного однозначного сообщения; не угадывать цель при ambiguity.
3. Proposed bounds r0.2 сохраняются: local admission ACK ≤250 ms, mute ≤300 ms, typed local status ≤500 ms, warm speech-end→preview ≤2.5 s (VAD включён), audible ACK ≤3.5 s. Default VAD 3 s уже превышает preview target, поэтому начать с manual record finish; не обещать SLO без измерения. Text-only first reply допустим для ограниченного smoke, но не audio-output PASS. Полная 12×2/10 campaign остаётся обязательной для полного S4 и не скрывается за новым smoke.
4. Затем ровно одна отдельно разрешённая synthetic read-only task через тот же receiving Hermes→Symphony contract: прочитать task-owned input.json и вернуть проверяемый результат. Никаких production/tracker writes. Новый Codex native context только после exact model/effort/placement/session permit. Старые три attempts не переиспользуются как новый grant.
5. Local voice.cancel/record stop, Hermes session.interrupt, Symphony job.cancel и emergency admission stop — разные события. Keyboard control и text input локальны; удалённый server не является единственным STOP/state owner. После reconnect или timeout UNKNOWN не вызывает новый request/native launch.

## Symphony integration HOLD

Свежий stdlib verifier подтверждает согласованность 46 сохранённых файлов старого smoke и recorded 144 tests: это не повторный inference/native smoke. Исторически read-only task и local tree stop+cancel replay прошли; общий cancel HOLD (billing/external outcomes UNKNOWN), старый лимит 3 native attempts исчерпан.

В сохранённом `smoke.py` timeout path около 192 вызывает `a.control.cancel_once()` напрямую; explicit cancel path около 275 проходит `owner.prepare_cancel(...)`. In-process cancel_once не доказывает общий durable dedupe после timeout/restart. Владелец сообщил необходимость исправления timeout dedupe; исходники/ledger этой линии не менять S4 writer. До receiving-contract readback, owner coordination и принятого timeout fix зависимый task smoke **HOLD**, даже если голос будет PASS. Не заявлять остановку серверной inference или settlement из Windows Job termination.

## Bimo и OpenWhispr fallback

Bimo — только заменяемый ASR backend через existing Hermes STT command/plugin interface, не executable queue. До remote speech нужны exact host/operator/transport, retention, request identity, cancel→server stop callback→terminal ACK и deadline. В текущем file-STT интерфейсе remote cancel не доказан. При loss Bimo локальные STOP/text доступны, late transcript не отправляется, state остаётся локально; без ACK ASR_CANCEL_UNKNOWN/HOLD. Ни credentials, ни аренда, ни remote/live audio этим manifest не разрешаются.

OpenWhispr не выключать/удалять/переконфигурировать. При выборе его single capture+ASR исключить запись Hermes и повторный STT; интеграция только адресным существующим API, если квалифицирован, без paste. Одновременно два capture path не включать.

## Откат и условия остановки

Остановить только собственные frontend/gateway процессы по identity, unregister только собственные hotkeys, закрыть собственный mic/stream; не трогать чужой gateway или Symphony. Сохранить state/results/UNKNOWN/audio согласно retention. Вернуться к неизменённому OpenWhispr/текстовому вводу. Не делать automatic `Remove-Item -Recurse`: удаление только exact disposable manifest после отдельного решения с проверкой ownership/absolute paths; общий Git/cache/user homes KEEP.

Неожиданный внешний model call/auth adoption, второй capture, wrong session, auto native start/retry, утрата local STOP или доступ к чужому home → affected phase HOLD. Новая модель, route, профили, privileged command, global network settings и source writes вне copy требуют явного scope decision.

## Следующая граница

Сейчас разрешены и выполнены только read-only checks и этот draft manifest. **INSTALL grant не выдан**, точный JS/toolchain download size/binary checksums и startup binding не закрыты. Code grant для global voice расширения не выдан. Независимый Context Hygiene этой stage delta не выполнен; same-session review не называется PASS. Новая Codex session/reviewer с неоговорённой моделью/средой не запускалась. Manifest не выдаётся за READY или разрешение установки. Следующее: завершить metadata/startup binding в доступном разрешённом контуре, подтвердить source-copy owner; затем независимый review и exact INSTALL/code grant. Mic/live speech и одна native task — отдельные последующие permits с названными получателями и моделью.
