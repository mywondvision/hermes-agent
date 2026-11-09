# Допуск на одну no-model кампанию

2026-11-09. Владелец ответил `yes` на предложение одной локальной no-model кампании: 8 targeted test-файлов, последовательно, без повторов, до 10 минут, в изолированных HOME/TEMP, с существующим test interpreter и fake port. Без запуска отдельного gateway, native worker, модели, микрофона, внешней речи, paid ресурсов, production и без установки dependencies.

Разрешён один вызов `run-contract-campaign.py` через штатный `scripts/run_tests.sh`. Source manifest сверён перед запуском: несовпадений нет; предыдущие log/result этой кампании отсутствовали. HEAD `8169f1b66a3a7c6844e3347c89f5cfa82edd4483`. В доступном app-server inventory текущий интегратор — единственная active сессия этого worktree; остальные serving/live сессии notLoaded. Это ограниченный snapshot, не доказательство отсутствия всех внешних OS writers.

Повторы campaign или file retries этим не разрешены. При материальном дефекте — сохранить результат, HOLD затронутого пути и определить консолидированную коррекцию без самовольного нового прогона. Review/correction/native budgets исходного CR не сбрасываются. Parent process timeout не доказывает quiescence потомков.
