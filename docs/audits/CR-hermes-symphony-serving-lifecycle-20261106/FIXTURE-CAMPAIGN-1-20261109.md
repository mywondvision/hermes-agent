# Fixture campaign 1: отказ окружения до тестов

Владелец ответил `ок` на один fixture-only проход пяти файлов, cap 600 s, без повторов/native/model/установки. Использован отдельный новый grant; прежние grants не переиспользовались.

Перед запуском совпали все 19 hashes кандидатного manifest. Дополнительно закреплены четыре прежних тестовых файла: campaign manifest содержит 23 файла. HEAD остался `16ea4b7abbb2541c61c7059f3bde6b3c784d261a`; ветка `codex/hermes-symphony-cancel-correction-20261106`.

Точная команда:

```text
python -X utf8 -I -B .artifacts/serving-windows-20261109/fixture-campaign-1/run-fixture-campaign.py
```

Обёртка вызвала `C:/Program Files/Git/bin/bash.exe scripts/run_tests.sh -j 1 --file-retries 0 --file-timeout 60` с пятью согласованными файлами, существующим test interpreter, отдельными HOME/runtime/TEMP, scrubbed env/offline/no lazy installs.

Через 0.313 s bash завершился с exit 256. MSYS сообщил `fatal error - CreateFileMapping … Win32 error 5`. Это отказ в доступе при старте Git Bash до исполнения test runner. Точная причина отказа доступа не установлена; автоматический approval review эту команду не отклонял.

**Тесты NOT_RUN; PASS/FAIL отсутствуют.** Native workers, модели, установка и повтор не запускались. Все 23 закреплённых файла совпали после попытки; индекс пуст, исходники не менялись.

Квитанции: `.artifacts/serving-windows-20261109/fixture-campaign-1/{campaign-plan.json,campaign-attempt.json,campaign.log,campaign-result.json,verification.json,syntax-and-source-manifest.json}`. Plan помечен `CONSUMED_ENVIRONMENT_STARTUP_FAILED`, remaining=0. Исторические результаты 114/24 не заменяют новую проверку.

Следующий operational шаг требует отдельного решения: один повтор той же fixture-only команды вне текущего ограниченного sandbox, с теми же пятью файлами, cap 600 s и без retries/native/model/install. Повышение доступа должно пройти штатный approval review; это предложение, не полученный допуск. Альтернативный запуск pytest или иной interpreter без согласования не выполняется.

CODE остаётся локальным кандидатом, native cancel HOLD. Crash harness и production bindings остаются незакрытыми, как описано в WINDOWS-CANDIDATE-20261109.md.
