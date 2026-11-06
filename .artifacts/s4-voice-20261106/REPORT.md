# CR-HERMES-MIGRATION-01 · S4 · безопасная предварительная проверка

Дата: 2026-11-06. Статус: **PRECHECK PARTIAL; S3 preparation/install NOT RUN; S4 live NOT RUN**.

Работа разрешена текущим запросом только как безопасная диагностика и подготовка плана. Установка, запись/передача речи, модельные сессии, аренда, production, ключи и изменение пользовательских профилей не выполнялись. Новый dispatcher не создавался. Наличие исходников `/voice` не является приёмкой.

## Применённые источники и ограничения

Пользователь предоставил точные пути, прочитанные без изменения:

- `C:/Users/Екатерина/Downloads/CR-HERMES-MIGRATION-01-r0.2-consolidated.md`: 272770 bytes, SHA256 `c8c5a2aee4ca1f2606ab0e104d205bf5adfe41ba695135ed7a55da85da7a067e`. Совпадает с ожидаемым digest существующего G1. Прочитаны полностью нормативный §6/§6.1 (193–220), S3/S4, включённая 18-строчная матрица A §6.2 и A §6.3–6.5 (659–721), 12 fixtures A §10.3 (984–992). Прежний gap доступа к этим разделам закрыт. Остальные 1855 строк не выдаются за повторный полный аудит.
- `C:/Users/Екатерина/Downloads/CR-HERMES-MIGRATION-01-r0.3-candidate-delta.md`: 13137 bytes, SHA256 `4be28257aae36555e9ae146838d3c15713235223b0b666a201e1a6da89c755fd`. Дополнение, а не замена r0.2. Сверено с ранее прочитанным G1 overlay: актуальный файл имеет `context_hygiene: PASS rev=50cf43562e07`, другой companion design digest `ce1e9409…`, дополнительный список оставшихся native/substrate/model/permit prerequisites. Он по-прежнему содержит `execution: NOT_AUTHORIZED_BY_THIS_DOCUMENT`. Hygiene не заменяет install/audio/session permit.
- Ранее доступный производный G1-пакет `D:/Codex/2026-10-31/task/symphony-worktree/.artifacts/g1-hermes-symphony-20261031/` использовался для навигации. Его старый overlay SHA256 `187f220e…` не подставляется вместо актуального файла пользователя. `owner-sources.json` фиксирует окончательные file identities; старые receipts не переписывались.
- Текущий Hermes checkout и его `website/docs/reference/package-management.md`, `pyproject.toml`, voice/STT исходники. Выводы относятся к этому HEAD, не к неподтверждённой установке или upstream main.

Сохранены требования: один frontend/capture owner/admission path; preview; raw отрицания и неоднозначности; локальный STOP вне LLM/сети; exact audio/model/placement/session grants; S3 отдельно от S4; без tracker/engineering effects; no blind retry. Полная S4 campaign остаётся 12×2 utterances + 10 control events по исходному CR; предложенный ниже smoke не заменяет её и не расширяет budget. S4 требует принятой S3 изоляции и двух отдельных native qualification sessions Claude/Codex с exact grants; эта диагностика их не запускает и не принимает S4 целиком. Whole-network-loss означает все egress только изолированного контура; глобальную сеть/Firewall/VPN ноутбука и параллельную Symphony работу не менять.

## Фактическое состояние

| Проверка | Результат |
|---|---|
| Исходный Hermes | detached HEAD `c419cf7c864ac0ee511dbd9f7091a8bba0b567ee`; tracked diff пуст |
| Готовая установка | **Не подтверждена**. `hermes` в PATH не найден; стандартный профиль `.hermes` не найден в проверенном текущем home. Это ограниченный отрицательный результат, не доказательство отсутствия на всех машинах |
| Доступный test interpreter | `D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe`, Python 3.14.9, AMD64 |
| Происхождение пакета | editable `hermes-agent 0.0.0` с `direct_url.json`, указывающим этот checkout. `0.0.0` — packaging placeholder; release установленного продукта этим не установлен |
| Audio/STT зависимости этого interpreter | Нет `sounddevice`, `numpy`, `faster-whisper`, `ctranslate2`; есть `openai 2.54.0`, pytest 9.1.1. Наличие SDK не означает наличия credentials/разрешённого provider |
| Пользовательский STT/profile/model route | **UNKNOWN**; не запускался config resolver с adoption/fallback/установкой; тела конфигов/credentials не извлекались |
| Микрофон | WinMM перечислил один вход, index 0, два канала, имя начинается `Набор микрофонов (Realtek(R) Au`. Только enumeration: `waveInOpen`/capture не вызывались. Windows ConsentStore сообщает Allow; это не допуск пользователя на запись в этой задаче |
| Локальный ASR asset | Найден файл `C:/Users/Екатерина/.cache/openwhispr/whisper-models/ggml-medium.bin`, 1533763059 bytes. В OpenWhispr resources/bin есть `whisper-server-win32-x64.exe`. Binary не запускался. Версия/лицензия/hash, endpoint, совместимость и latency UNKNOWN |
| Совместимость модели | GGML asset не является готовой моделью faster-whisper/CTranslate2. В проверенных стандартных HF/Whisper cache roots модели не найдены; это не полный поиск дисков |
| TUI | Исходники есть, `ui-tui/dist` и корневого `node_modules` нет; готовый TUI build не найден |
| Живая речь, русский, числа, отрицания, шум/тишина, ответ, stop recording | **NOT RUN**, задержки **UNKNOWN** |

Два Git worktree зарегистрированы: текущий detached checkout и `D:/Codex/codex-home/worktrees/cf6e/hermes-pm-ingress-20261031` на `369a012c…`; доступной `docs/` второго пути не найдено. Регистрация не доказывает запуск runtime. Параллельный PM/ingress scope не изменялся. 13 выбранных исходных/чужих файлов совпали по SHA256 до/после; это выборочный readback, не гарантия отсутствия стороннего writer. Подробности: `inventory.json`, `readback.json`.

## Три риска по фактическим исходникам

| Риск | Проверенный контракт исходников | Приёмка |
|---|---|---|
| Редактируемый preview | `ui-tui/src/app/createGatewayEventHandler.ts`, строки около 523: `voice.submit_mode: draft` помещает текст в composer, не вызывает submit. Default `direct` отправляет автоматически. Classic `hermes_cli/cli_voice_mixin.py:232` кладёт transcript сразу в `_pending_input`; preview там не показан | TUI draft — кандидат; фактический профиль/build/runtime UNKNOWN |
| WAV при STT failure | Classic `_voice_stop_and_transcribe`, около 250: при `transcription_failed` оставляет WAV и печатает путь. `tools/voice_mode.py:521` создаёт `recording_YYYYMMDD_HHMMSS.wav` в `tempfile.gettempdir()/hermes_voice`. Barge-in classic удаляет WAV в finally. TUI `hermes_cli/voice.py:_transcribe_wav` удаляет WAV в finally даже при ошибке. `cleanup_temp_recordings` имеет default age 3600 s; наличие сохранённого файла не обещает бессрочную retention | Сохранение не универсально. TUI failure retention — неподтверждённое/неудовлетворённое требование; исправления не делались |
| Отмена удалённого ASR | `stop_continuous(False)` отключает цикл и cancel recorder; file STT вызывается синхронно, cancel token/remote ACK в его вызов не передаётся. TUI `_voice_capture_callback` отсекает поздний callback по generation. Это не остановка server computation. Cloud OpenAI helper по умолчанию timeout 60 s/max_retries 1. Classic после завершения STT может поставить текст в очередь без повторной проверки voice-off | Удалённая остановка **UNKNOWN / не доказана**. UI off, request timeout и server terminal — отдельные события |

Дополнительно: default STT language `en`, default stop phrase только `stop`; русское `стоп` без настройки не обещано. Голосовой STOP требует распознавания и не заменяет локальный keyboard/control STOP. Defaults VAD silence 3 s и max recording 120 s — параметры исходников, не измеренные задержки. Silence window 3 s уже превышает proposed warm end-of-speech→preview 2.5 s до учёта STT; поэтому первый smoke с ручным PTT stop, без обещания достижения SLO. Для smoke предлагаем короткий cap, `ru`, отключённые retries/streaming/TTS/client_direct; ничего из этого в пользовательский профиль не применено.

## Выполненные команды и результаты

```powershell
Get-Command hermes,python,uv -ErrorAction SilentlyContinue
git --no-optional-locks status --short
git rev-parse HEAD
git worktree list --porcelain
& 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe' -I -B .artifacts/s4-voice-20261106/inventory.py
& 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe' -I -B .artifacts/s4-voice-20261106/run-safe-checks.py
```

`inventory.py`: exit 0, внутреннее время 0.414 s; это инвентаризация, не STT latency. Вывод и source hashes в `inventory.json`.

`run-safe-checks.py` сделал одну попытку существующих mocked contracts через обязательный runner:

```powershell
& 'C:/Program Files/Git/bin/bash.exe' scripts/run_tests.sh tests/hermes_cli/test_voice_wrapper.py tests/tools/test_voice_cli_integration.py tests/tools/test_voice_stop_phrase.py -j 1 --file-retries 0
```

Interpreter pinned; отдельные task-owned HOME/TEMP/runtime/test config, credentials исключены из дочернего окружения, lazy installs отключены, STT disabled, offline flags. Это disposable test fixtures, не пользовательский профиль. Exit 256, 0.060 s: MSYS `CreateFileMapping ... Win32 error 5`, **до тестов**. Тесты NOT RUN, не product FAIL/PASS. Логи `canonical-runner.*`. Обход через bare pytest, ACL change, reinstallation или повторный shell не применялся.

## Минимальный S3 план, пока без исполнения

1. r0.2 и актуальный r0.3 теперь доступны и привязаны к SHA. Следующее — сверить актуальный stage permit/S2 acceptance для затронутых contracts. Уточнить единственного writer и отдельный каталог source/build: в текущем checkout работают параллельно, а TUI build пишет файлы. Не запускать найденные бинарники ради discovery.
2. Зафиксировать manifest: этот HEAD/lock, Windows AMD64, текущий Python 3.14, выбранные версии, каталоги записи, модели с hash/license/size, память, network/download scope. Сначала проверить reuse существующих assets; OpenWhispr server не объявлять совместимым автоматически. Предпочтение — встроенный local STT после qualification. Проверить доступность exact wheel closure и полный объём до установки; не применять `--all-extras`/общий setup вслепую.
3. После отдельного допуска на preparation/install создать отдельные home/runtime и PM environment. PM поддерживает узкий frozen build с extras `audio-io` и `stt-whisper`, без изменения существующего test interpreter и без raw pip/uv. Пример **предложения**, не выполненная команда; пути должны быть подтверждены permit, каталог `env` должен отсутствовать:

```powershell
$s4Root = 'D:/AI/worktrees/hermes-pm-ingress-20261031/.artifacts/s4-voice-20261106/prepared'
$env:HERMES_HOME = Join-Path $s4Root 'profile'
$env:HERMES_RUNTIME_DIR = Join-Path $s4Root 'runtime'
& 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe' -B -m pm.build_env --source . --out "$s4Root/env" --python 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe' --extra audio-io --extra stt-whisper --offline
```

Это план для отдельного разрешённого shell; environment variables не менялись текущей проверкой. `--offline` закрывает network; при cache miss HOLD, не повторять автоматически с сетью. PM build не даёт готовую модель и не собирает JS TUI. TUI dependency/build — отдельные точные команды/каталог в install manifest; общий `activate.ps1` намеренно не запускался: по документации он provisions toolchain и sync `all`.

4. В новом изолированном smoke profile предложить `security.allow_lazy_installs: false`, `stt.provider: local`, `stt.language: ru`, `stt.local.language: ru`, CPU/int8 для первого ограниченного теста, `stt.streaming: false`, `voice.submit_mode: draft`, `voice.auto_tts: false`, `voice.client_direct: false`, `voice.barge_in: false`, `voice.max_recording_seconds: 15`, `wake_word.enabled: false`; model/cache path только после qualification. Не копировать auth/config/memory/plugins из чужих homes. Запретить external-login adoption, goal/kanban/cron/PM dispatch, hooks и undeclared auxiliary routes; точные поддерживаемые настройки и OS boundary требуют проверки перед применением. Подтвердить отсутствие tool execution, автоматического resume/new-session и model fallback перед отправкой.
5. TUI launch candidate после готовности manifest: `<approved-env>/Scripts/hermes.exe --tui`. Это не безопасная самодостаточная команда: **не запускать**, пока не доказаны изоляция, нулевые executable tools/dispatch, запрет автоустановок/авторезюма и выбранный model route. Не подставлять выдуманный `--no-tools` или `--toolsets none`; такой безопасный CLI контракт здесь не установлен.

## Минимальный живой smoke после отдельного audio/session допуска

Один frontend — встроенный TUI, chained/file STT, один input handler. Сначала **микрофон → local STT → редактируемый draft**, без Enter/model call. Только затем один явно отправленный безопасный текст: «Ответь одним словом: принято». Ответ текстом, TTS off. Для model route заранее назвать получателя transcript, если он не локальный; подключение Bimo/облака автоматически не следует из допуска на локальную запись.

Ниже новые короткие smoke-примеры, **не подмена 12 исходных fixtures CR**:

| Случай | Пример / oracle |
|---|---|
| Русский | «Это проверка распознавания русского языка»; читабельный draft |
| Числа и отрицания | «Число 125, не 152. Ничего не запускай»; цифры семантически верны, оба запрета сохранены |
| Самокоррекция | «Двадцать один, исправляю: двенадцать»; correction видна, цель не угадана |
| Неоднозначность | «В той задаче ничего не меняй»; никаких действий, при отправке только clarification |
| Тишина | 5 s без речи; пустой draft, ноль отправок/задач |
| Шум | Разрешённый неперсональный фон без речи; ноль команд, любой phantom transcript = HOLD |
| Остановка записи | Ctrl+B начать/закончить, проверить освобождение input; `/voice off` локально. В TUI record stop может отправить запись на STT, поэтому это не equivalent cancel |
| Отмена и потеря сети | Во время STT локальный STOP и текст остаются доступны, late transcript не submit; отдельно доказать remote server cancel ACK/terminal, если remote вообще разрешён |

Метки времени: start capture, key release/manual stop, VAD stop, final transcript/draft, explicit submit, first/final answer, local STOP ACK, remote cancel ACK/terminal. Отдельно cold/warm STT и VAD tail. По полному r0.2 proposed bounds: local admission/ACK ≤250 ms, playback mute ≤300 ms, typed/local status ≤500 ms, cancel request ≤1 s, local stop либо честный HOLD ≤2 s, native ACK до 10 s, ≤2 readbacks за ≤30 s, warm speech-end→preview ≤2.5 s, первый короткий audible ACK ≤3.5 s. Все это **предложенные пороги, не измеренные факты и не автоматически принятые SLO**. В text-only smoke audible ACK/TTS не проверяется. При тишине/неоднозначности не тратить model turn. Эти пробы сначала без внешних действий; при ошибке сохранять evidence/UNKNOWN, без повторов в обход budget. Полная 12×2/10 приёмка остаётся отдельной стадией.

Исходные 12 fixtures теперь прочитаны: bounded task, status по имени, status «эта задача», resume exact job, stop exact job, emergency STOP, «не реализуй», «не коммить», «production не трогать», неоднозначный проект, self-correction, шум/ложная активация. Их oracle — intent/constraints и ноль опасных dispatch, не буквальная транскрипция. В текущем smoke tasks/native scenarios не выполняются. Все 18 строк A §6.2 сохраняют runtime NOT_RUN; channel voice, global hotkey/minimized/locked behavior, desktop mode, TTS/barge-in, task mapping/resume/native cancel, whole-network offline, wake/auxiliary/credential egress не принимаются наличием TUI draft. Для выбранного reduced smoke используется focused PTT; это требует явного owner scope, не global-hotkey PASS.

Допуск на микрофон сейчас не запрашивается по последней инструкции пользователя. Для будущего локального smoke получатель аудио — только названный локальный Hermes/STT процесс; для Bimo требуется отдельно назвать точный host/operator/transport, retention и cancellation contract. До этого запись/передача запрещены.

## Если local STT окажется недостаточным: узкий Bimo ASR

Условное предложение, не принятое решение: Hermes остаётся локальным frontend/state/control owner; Bimo выполняет только ASR. В Hermes уже есть `stt.providers.<name>: type: command` и `TranscriptionProvider`/`TranscriptionStreamSession` extension interfaces. В проверенном Bimo `evals/bimo_compute_p3/asr_backend.py` уже есть `open(lang)`, `feed(pcm, stop)`, `finish(stop)` для 16 kHz mono PCM и отдельный buffered fallback. Это source evidence, не готовый удалённый API/совместимость.

Предпочтительный первый bridge — bounded file STT через существующий command/provider interface после подтверждения receiving API. Никакого второго dispatcher, очереди задач, переноса состояния или keys/access создания. Не выдумывать OpenAI-compatible endpoint: его наличие не подтверждено. `TranscriptionStreamSession` сам по себе не предоставляет абстрактного remote-cancel метода; Bimo callback не означает, что UI STOP уже до него доходит.

Для remote acceptance нужны request identity, ноль автоматических retries, deadline, явное cancel → backend stop callback → terminal ACK для того же request, запрет позднего commit/submit и retention policy. Разрыв HTTP/kill локального client не доказывает остановку сервера. При отсутствии ACK: ASR_CANCEL_UNKNOWN/HOLD, локальные keyboard STOP/text input работают независимо. Плата/аренда не стартуют. Lost Bimo не запускает другую модель или новый dispatcher. Local failure retention для выбранного TUI требует отдельной bounded delta/CR gate; это не исправлено данной диагностикой.

Ближайший шаг: конечный S3 manifest/preparation permit с точными source/build/home/runtime/model и download scope, без микрофона. Источники r0.2/r0.3 уже получены и проверены. До отдельного разрешённого runtime smoke installation/voice/provider latency/remote cancel остаются UNKNOWN/NOT RUN; голос S4 не принят.
