# CODE-поправка: host binding Hermes → Symphony

Статус: **HOST BINDING CONTRACT PASS (E1), полный serving transport NOT_READY**. Разрешённая no-model кампания: **114 PASS / 0 FAIL**, 8 файлов, один запуск, без file retries. Runner 132.1 s, команда 135.703 s. Это unit/contract evidence с реальными plugin discovery/registry и fixture-only issuer/port; installed/native/voice runtime приёмки нет. Канонический CR — `docs/change-requests/CR-hermes-symphony-serving-lifecycle-20261106.md`, approved normalized revision `d408f7b5b619e13ab13fd113b428b7e5633399719d0b7732df7fa66a8d2d691d`. `ок` владельца на CODE-поправку записан отдельно в `.artifacts/serving-lifecycle-implementation-20261109/code/CODE_APPROVAL.md`, последующий `yes` на кампанию — в `CAMPAIGN_APPROVAL.md`. Содержательная ревизия CR и независимый отчёт не менялись.

## Что добавлено

- Generic `ctx.register_runtime_service`: discovery создаёт только декларацию; явный owning host запускает factory. Scope фиксирует профиль, namespace, generation и host binding. Есть идемпотентный start, отказ второму host, закрытие admission, cleanup в owning profile, отсутствие автоматического retry после startup failure и невозможность retarget старой ссылки на новую generation.
- Host call sites: запуск messaging gateway, secondary profile bootstrap, TUI stdio и standalone HTTP lifespan. HTTP, присоединённый к готовому gateway, не создаёт второй host. Shutdown/unload закрывают admission прежде освобождения зарегистрированных ресурсов. Callback получает deadline; зависший plugin callback по-прежнему требует существующего host watchdog, а не новой службы.
- Revocable request scope проверяет точные profile/namespace/session/request/digest/issuer/grant/contract/expiry. Scope не наследуется посторонним thread/detached task и отзывается при выходе или остановке service.
- Существующий `symphony_pm` — конкретный consumer. Он получает session ID из host-owned registry kwargs, без fallback на process env или tool args. Port и approval source внедряются host до bootstrap. По умолчанию они отсутствуют: PM tool остаётся UNAVAILABLE. Отказ/сбой issuer не использует authority внешнего request scope. Существующая adapter schema не менялась.
- Pinned `plugins.isolation: host` отказывает live-service регистрации. Настройки и профили оператора, установленный S3 contour, OpenWhispr, Bimo/kit worktree не менялись. Нового dispatcher, scheduler, queue, RPC или HTTP endpoint нет.

## Проверки и граница утверждений

До кампании выполнены AST parse/compile без импорта project modules и `git diff --check`. Source manifest: `.artifacts/serving-lifecycle-implementation-20261109/code/syntax-and-source-manifest.json`. После кампании все 18 source hashes совпали с этим manifest; во время проверки код не менялся. HEAD остаётся `8169f1b66a3a7c6844e3347c89f5cfa82edd4483`, индекс пуст.

Проверены четыре новых test-файла: lifetime/unload/failure/two profiles/thread/task/expiry/isolation; real plugin discovery + fake issuer/port + adapter; TUI shutdown order; HTTP attached/standalone ownership. Вместе с четырьмя regression-файлами они дали следующий результат:

| Файл | PASS | Время файла |
|---|---:|---:|
| `tests/hermes_cli/test_plugin_services.py` | 16 | 20.7 s |
| `tests/hermes_cli/test_web_server_plugin_services.py` | 2 | 12.2 s |
| `tests/plugins/test_symphony_pm_ingress_offline.py` | 38 | 33.7 s |
| `tests/plugins/test_symphony_pm_plugin.py` | 10 | 18.4 s |
| `tests/plugins/test_symphony_pm_serving.py` | 4 | 11.9 s |
| `tests/tools/test_symphony_pm_adapter.py` | 35 | 15.1 s |
| `tests/tools/test_symphony_pm_adapter_cancel_s2.py` | 7 | 11.5 s |
| `tests/tui_gateway/test_plugin_service_lifecycle.py` | 2 | 8.2 s |

Квитанции и точные argv/результаты: `.artifacts/serving-lifecycle-implementation-20261109/code/{contract-campaign.json,contract-campaign.log,campaign-proof.json}`. Эти времена относятся к тестам, не к голосовой latency. Static discovery оценивал ~86 tests, фактическая pytest summary — 114; итог берётся из summary. Все файлы прошли в первой попытке.

Ровно одна локальная кампания выполнена на 8 targeted files через штатный runner, `-j 1`, без file retries, в выделенных HOME/TEMP, с существующим test interpreter, без установки dependencies. Ограничение wrapper — 600 s. Native worker, новый gateway/runtime, модель, микрофон, внешний голос, GPU/paid ресурсы и production не запускались. Canonical runner создавал изолированные pytest subprocesses; тесты использовали только in-process fake port и ограниченные thread/async fixtures. Historical C1→C2, native/model smoke и аренда не повторялись.

Выполненная команда (разрешённый запуск израсходован; повтор не требуется):

```powershell
python -X utf8 -I -B .artifacts/serving-lifecycle-implementation-20261109/code/run-contract-campaign.py
```

Wrapper вызывает:

```text
bash scripts/run_tests.sh -j 1 --file-retries 0 --file-timeout 60
  tests/hermes_cli/test_plugin_services.py
  tests/hermes_cli/test_web_server_plugin_services.py
  tests/plugins/test_symphony_pm_serving.py
  tests/tui_gateway/test_plugin_service_lifecycle.py
  tests/tools/test_symphony_pm_adapter.py
  tests/tools/test_symphony_pm_adapter_cancel_s2.py
  tests/plugins/test_symphony_pm_plugin.py
  tests/plugins/test_symphony_pm_ingress_offline.py
```

600 s timeout не доказывает остановку descendants: при нём результат UNKNOWN, без автоматического повторения. Существующие per-file timeout правила runner могут увеличивать 60 s по историческим timings; общий cap wrapper сохраняется. Новый допуск/кампания не сбрасывает correction budget parent CR. При найденных материальных дефектах — HOLD затронутого пути и один консолидированный checkpoint, не бесконечный rerun.

## Что остаётся HOLD / UNKNOWN

Production approval source и его связь с exact model/environment/attempt permit **UNKNOWN**. Production journal, OS observer/Job lifecycle, atomic native fencing и durable recovery этой CODE-поправкой не реализованы и не квалифицированы; это оставшийся исходный serving scope. Callback deadline — кооперативный контракт, не доказательство прекращения native work.

Legacy standalone TUI/HTTP поднимает service для launch profile. Дополнительные legacy profile mounts и live rediscovery требуют явного safe host reconciliation; диагностический lookup их не запускает. Canonical messaging host имеет отдельный secondary-profile bootstrap. Нельзя выдавать эти пути за одну принятую матрицу без проверок.

Installed runtime остаётся на `c419cf7…` без текущей Symphony интеграции. Generic CODE не установлен. Глобальная voice-клавиша, живой русский голос/ответ и полный Hermes→Symphony сценарий не приняты. Общий cancel/release **HOLD**; ASR cancel и остановка записи не доказывают остановку worker.

Дальнейших запусков этой кампании не разрешено и для уже проверенных свойств не требуется. Следующий участок исходного CR — production journal/lifecycle и закрепление production approval source с exact request/native permit binding; они не возникают из fixture PASS. Сборка/установка, native pilot, mic/voice-transfer, commit/push/merge остаются отдельными действиями.
