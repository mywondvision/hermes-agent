# Разрешение focused no-model campaign

2026-11-09. Пользователь ответил `да` на точный вопрос: «Разрешаете один no-model проход: 4 файла, до 600 секунд, без повторов, моделей, workers, микрофона и установки?»

Разрешён один запуск подготовленного `run-transport-campaign.py` с source hash readback и canonical `scripts/run_tests.sh`: journal, lifecycle, permit source, serving discovery/bootstrap. Выделенные HOME/runtime/TEMP, существующий test interpreter, offline, lazy installs disabled. Это разрешает pytest subprocesses и fixture callbacks; не разрешает native worker/model, gateway/runtime start, live UI approval delivery, microphone/audio transfer, paid resources, installation, production, commit/push либо повтор старой 8-файловой кампании. После начала attempt бюджет этого прохода израсходован независимо от результата; автоматического retry нет.
